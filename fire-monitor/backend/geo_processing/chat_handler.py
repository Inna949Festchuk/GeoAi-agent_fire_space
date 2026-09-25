"""
AI chat handler — processes user messages and calls appropriate tools.

Uses OpenAI-compatible API (Qwen3.7-plus via routerai.ru) with function calling
to determine which fire monitoring tools to invoke.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pyproj import Transformer
from shapely.ops import transform as shapely_transform
from django.conf import settings
from openai import OpenAI

from .firms_client import fetch_active_fires
from .fire_filter import filter_fire_hotspots, get_filter_summary
from .stac_client import search_sentinel2
from .routing import (
    build_route, build_route_cached, build_routes_batch, find_nearest_fire_stations,
    haversine_distance, add_route_length_labels, geojson_line_length_km,
)

logger = logging.getLogger(__name__)


TOOL_RESULT_TTL_SEC = 3600  # результаты инструментов доступны для инъекций 1 час

# Общие данные последних вызовов между итерациями одного запроса (и между
# запросами в рамках TTL) — чтобы LLM не копировал GeoJSON/координаты вручную:
#   {'search_fires': gj, 'routes_geojson': gj, 'fires_geojson': gj}
_last_tool_data: dict = {}
_last_tool_data_ts: float = 0.0


def _remember_tool_data(func_name, result):
    """Кеширует geojson/map_data результата инструмента для авто-инъекций."""
    global _last_tool_data, _last_tool_data_ts
    gj = result.get('geojson') or (result.get('map_data') or {}).get('data')
    if not isinstance(gj, dict):
        return
    _last_tool_data[func_name] = gj
    if func_name == 'search_fires':
        _last_tool_data['fires_geojson'] = gj
    if func_name in ('dispatch_routes_to_fires', 'build_routes_batch', 'build_route'):
        _last_tool_data['routes_geojson'] = gj
    _last_tool_data_ts = time.time()


def _recall_tool_data(*keys):
    """Возвращает закешированный GeoJSON по первому найденному ключу (или None)."""
    if time.time() - _last_tool_data_ts > TOOL_RESULT_TTL_SEC:
        return None
    for k in keys:
        gj = _last_tool_data.get(k)
        if isinstance(gj, dict) and gj.get('features'):
            return gj
    return None


def _args_for_log(func_name, func_args):
    """Сокращает аргументы для логов/действий: не зацикливает весь GeoJSON."""
    if func_name == 'dispatch_routes_to_fires' and isinstance(func_args.get('fires_geojson'), dict):
        args = dict(func_args)
        gj = args['fires_geojson']
        n = len(gj.get('features', [])) if isinstance(gj.get('features'), list) else 0
        args['fires_geojson'] = f'<FeatureCollection: {n} features>'
        return args
    return func_args


def _dispatch_routes_to_fires(args):
    """
    Автодиспетчеризация: группирует пожары в очаги, для каждого очага находит
    ближайшую пожарную часть (Overpass) и строит маршрут по дорогам (OSRM).
    Возвращает сводку с координатами (чтобы LLM видел их) и GeoJSON маршрутов+станций.
    """
    fires_gj = args.get('fires_geojson')
    radius_km = args.get('radius_km') or 100
    cluster_km = args.get('cluster_km') or 5
    max_routes = args.get('max_routes') or 10

    if not isinstance(fires_gj, dict):
        return {'summary': {'error': (
            'fires_geojson is required — call search_fires first '
            '(the system injects its result automatically)'
        )}}
    fire_features = [
        f for f in fires_gj.get('features', [])
        if isinstance(f, dict) and isinstance(f.get('geometry'), dict)
        and f['geometry'].get('type') == 'Point'
        and isinstance(f['geometry'].get('coordinates'), list)
        and len(f['geometry']['coordinates']) >= 2
    ]
    if not fire_features:
        return {'summary': {'error': 'No fire points found in fires_geojson'}}

    # 1. Кластеризация близких точек пожара в очаги (жадный алгоритм по haversine)
    clusters = []  # [{'lon','lat','frp','count'}]
    for feat in fire_features:
        lon, lat = feat['geometry']['coordinates'][0], feat['geometry']['coordinates'][1]
        frp = (feat.get('properties') or {}).get('frp') or 0
        target = None
        for c in clusters:
            if haversine_distance(c['lat'], c['lon'], lat, lon) <= cluster_km:
                target = c
                break
        if target is None:
            clusters.append({'lon': lon, 'lat': lat, 'frp': float(frp), 'count': 1})
        else:
            n = target['count']
            target['lon'] = (target['lon'] * n + lon) / (n + 1)
            target['lat'] = (target['lat'] * n + lat) / (n + 1)
            target['frp'] += float(frp)
            target['count'] = n + 1

    # 2. Сортировка очагов по мощности (FRP), ограничиваем число маршрутов
    clusters.sort(key=lambda c: c['frp'], reverse=True)
    clusters = clusters[:max_routes]

    # 3. Для каждого очага — ближайшая часть + маршрут от неё к очагу
    route_pairs = []
    stations_used = []
    skipped = []
    for c in clusters:
        st = find_nearest_fire_stations(
            lat=c['lat'], lon=c['lon'], radius_km=radius_km, limit=1
        )
        if not st.get('success') or not st.get('stations'):
            skipped.append({
                'fire': [round(c['lon'], 5), round(c['lat'], 5)],
                'reason': st.get('error', 'no stations found'),
            })
            continue
        s = st['stations'][0]
        route_pairs.append([[s['lon'], s['lat']], [c['lon'], c['lat']]])
        stations_used.append(s)

    if not route_pairs:
        return {'summary': {
            'error': f'No fire stations found within {radius_km} km of any fire cluster',
            'fire_clusters': len(clusters),
            'skipped': skipped[:5],
        }}

    routes_result = build_routes_batch(route_pairs)
    if not routes_result.get('success'):
        return {'summary': {'error': 'Failed to build routes via OSRM',
                            'details': routes_result.get('errors')}}

    # 4. Объединяем GeoJSON: маршруты + точки ближайших частей
    features = list(routes_result['geojson'].get('features', []))
    for i, s in enumerate(stations_used):
        features.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [s['lon'], s['lat']]},
            'properties': {
                'name': s.get('name') or f'Пожарная часть {i + 1}',
                'address': s.get('address', ''),
                'phone': s.get('phone', ''),
                'distance_km': s.get('distance_km'),
                'type': 'fire_station',
            },
        })
    combined_geojson = {'type': 'FeatureCollection', 'features': features}

    # 5. Сводка с КОРОТКИМИ координатами — она уходит обратно в LLM,
    #    поэтому её можно безопасно использовать в следующих вызовах инструментов
    routes_summary = []
    for i, ((st_lon, st_lat), (f_lon, f_lat)) in enumerate(route_pairs):
        routes_summary.append({
            'route': i + 1,
            'station': [round(st_lon, 5), round(st_lat, 5)],
            'station_name': stations_used[i].get('name', ''),
            'fire': [round(f_lon, 5), round(f_lat, 5)],
        })

    summary = {
        'success': True,
        'fire_clusters': len(clusters),
        'routes_built': routes_result['successful_routes'],
        'total_distance_km': routes_result['total_distance_km'],
        'total_duration_min': routes_result['total_duration_min'],
        'routes': routes_summary,
        'message': (
            f"Построено {routes_result['successful_routes']} маршрутов от ближайших "
            f"пожарных частей до очагов пожаров (суммарно "
            f"{routes_result['total_distance_km']} км, "
            f"{routes_result['total_duration_min']} мин)."
        ),
    }
    if skipped:
        summary['skipped'] = skipped

    return {
        'summary': summary,
        'geojson': combined_geojson,
        'data_type': 'routes',
    }

TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'search_fires',
            'description': 'Search for active fire hotspots from satellite data (VIIRS, MODIS). Returns filtered fire locations with brightness, confidence, and FRP.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'bbox': {
                        'type': 'array',
                        'items': {'type': 'number'},
                        'description': 'Bounding box [minx, miny, maxx, maxy] in WGS84',
                    },
                    'source': {
                        'type': 'string',
                        'enum': ['VIIRS_SNPP', 'VIIRS_NOAA20', 'MODIS_Aqua', 'MODIS_Terra', 'all'],
                        'description': 'Satellite source',
                    },
                    'days': {
                        'type': 'integer',
                        'description': 'Number of days to look back (1 or 10)',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_sentinel2_scenes',
            'description': 'Search for Sentinel-2 satellite scenes available for a given area and time range. Useful for planning burn severity analysis.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'bbox': {
                        'type': 'array',
                        'items': {'type': 'number'},
                        'description': 'Bounding box [minx, miny, maxx, maxy] in WGS84',
                    },
                    'start_date': {
                        'type': 'string',
                        'description': 'Start date (YYYY-MM-DD)',
                    },
                    'end_date': {
                        'type': 'string',
                        'description': 'End date (YYYY-MM-DD)',
                    },
                    'max_cloud_cover': {
                        'type': 'integer',
                        'description': 'Maximum cloud cover percentage (0-100)',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_fire_statistics',
            'description': 'Get aggregated fire statistics for a region: total hotspots, by source, by confidence level.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'bbox': {
                        'type': 'array',
                        'items': {'type': 'number'},
                        'description': 'Bounding box [minx, miny, maxx, maxy] in WGS84',
                    },
                    'days': {
                        'type': 'integer',
                        'description': 'Number of days to look back',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'map_burn_area',
            'description': 'Map burn severity using Sentinel-2 dNBR analysis. Requires pre-fire and post-fire dates. Returns burn area polygons with severity classification. IMPORTANT: bbox must be max 30° × 30° (approx 3000km × 3000km). For larger regions, split into multiple calls or ask user to specify a smaller area.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'bbox': {
                        'type': 'array',
                        'items': {'type': 'number'},
                        'description': 'Bounding box [minx, miny, maxx, maxy] in WGS84. MUST be max 30° × 30° (e.g., [79, 71, 109, 101]). For larger regions, split into multiple calls.',
                    },
                    'pre_date': {
                        'type': 'string',
                        'description': 'Pre-fire date (YYYY-MM-DD) - before the fire',
                    },
                    'post_date': {
                        'type': 'string',
                        'description': 'Post-fire date (YYYY-MM-DD) - after the fire',
                    },
                    'max_cloud_cover': {
                        'type': 'integer',
                        'description': 'Maximum cloud cover percentage (default: 30)',
                    },
                },
                'required': ['bbox', 'pre_date', 'post_date'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'generate_burn_report',
            'description': 'Generate a comprehensive analytical report for burn severity analysis. Includes severity distribution, summary statistics, and data quality metrics. Requires existing burn area data in the database.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'bbox': {
                        'type': 'array',
                        'items': {'type': 'number'},
                        'description': 'Bounding box [minx, miny, maxx, maxy] in WGS84',
                    },
                    'date_from': {
                        'type': 'string',
                        'description': 'Start date (YYYY-MM-DD)',
                    },
                    'date_to': {
                        'type': 'string',
                        'description': 'End date (YYYY-MM-DD)',
                    },
                },
                'required': ['bbox', 'date_from', 'date_to'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'execute_python',
            'description': 'Execute custom Python code for geospatial analysis. Runs in ultra-hardened sandbox with numpy (np), pandas (pd), geopandas (gpd), shapely, pyproj (CRS, Transformer, Geod), rasterio + GDAL/osgeo (gdal, ogr, osr), xarray/rioxarray (xr) pre-imported. NO import statements needed. To display results on map, assign GeoJSON to __result__. Use print() for text output.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'code': {
                        'type': 'string',
                        'description': 'Python code WITHOUT imports. Example: gdf = gpd.GeoDataFrame.from_features(context["fires"]["features"]); __result__ = gdf.__geo_interface__',
                    },
                    'context': {
                        'type': 'object',
                        'description': 'Variables to inject (e.g., pass GeoJSON from previous tool calls). Max 10MB.',
                    },
                },
                'required': ['code'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'build_route',
            'description': 'Build a driving route between two points using OSRM (Open Source Routing Machine). Returns GeoJSON LineString with route geometry, distance, and duration.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'start': {
                        'type': 'array',
                        'items': {'type': 'number'},
                        'description': 'Start point [longitude, latitude]',
                    },
                    'end': {
                        'type': 'array',
                        'items': {'type': 'number'},
                        'description': 'End point [longitude, latitude]',
                    },
                },
                'required': ['start', 'end'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'build_routes_batch',
            'description': 'Build multiple driving routes at once using OSRM. Use this when you need routes from multiple fire stations to multiple fires. Returns combined GeoJSON with all routes.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'route_pairs': {
                        'type': 'array',
                        'items': {
                            'type': 'array',
                            'items': {
                                'type': 'array',
                                'items': {'type': 'number'},
                                'description': '[lon, lat]'
                            },
                            'description': 'Pair of [start, end] coordinates'
                        },
                        'description': 'List of [start, end] pairs. Example: [[[37.6, 55.7], [37.9, 55.8]], [[37.5, 55.6], [37.8, 55.9]]]',
                    },
                },
                'required': ['route_pairs'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'dispatch_routes_to_fires',
            'description': 'AUTO-DISPATCH: Build optimal driving routes (OSRM) from the nearest fire station to each fire hotspot in ONE call. USE THIS TOOL whenever the user asks to build routes from fire stations to fires (e.g. "построй оптимальный маршрут от пожарных частей к пожарам"). It automatically clusters fire points, finds the nearest station for each cluster via Overpass and builds all routes via OSRM — do NOT try to extract coordinates yourself or use execute_python for this task.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'fires_geojson': {
                        'type': 'object',
                        'description': 'GeoJSON FeatureCollection of fire points (the result of search_fires). OPTIONAL — if omitted, the result of the previous search_fires call is used automatically.',
                    },
                    'radius_km': {
                        'type': 'integer',
                        'description': 'Max radius to search for fire stations around each fire (default: 100)',
                    },
                    'cluster_km': {
                        'type': 'number',
                        'description': 'Distance threshold in km to merge nearby fire hotspots into one fire cluster (default: 5)',
                    },
                    'max_routes': {
                        'type': 'integer',
                        'description': 'Maximum number of routes to build (default: 10)',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'label_route_lengths',
            'description': 'Add length labels (км) on the map for previously built routes — in ONE fast call. USE THIS TOOL whenever the user asks to label/annotate route lengths or distances on the map (e.g. "подпиши длину маршрутов"). It reuses routes already built by dispatch_routes_to_fires / build_routes_batch (the result is injected automatically) and instantly places a text label at the midpoint of each route. NEVER use execute_python for this task — recomputing geometry there is slow and error-prone.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'routes_geojson': {
                        'type': 'object',
                        'description': 'GeoJSON FeatureCollection with route LineStrings. OPTIONAL — if omitted, the GeoJSON from the previous routing call (dispatch_routes_to_fires / build_routes_batch / build_route) is used automatically.',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'find_nearest_fire_stations',
            'description': 'Find fire stations using OpenStreetMap (Overpass API). Can search around a point or in a bounding box. Returns GeoJSON with fire station locations and distances.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'lat': {
                        'type': 'number',
                        'description': 'Latitude of search center (required if bbox not provided)',
                    },
                    'lon': {
                        'type': 'number',
                        'description': 'Longitude of search center (required if bbox not provided)',
                    },
                    'radius_km': {
                        'type': 'integer',
                        'description': 'Search radius in kilometers (default: 50, used only with lat/lon)',
                    },
                    'limit': {
                        'type': 'integer',
                        'description': 'Maximum number of stations to return (default: None = return all found)',
                    },
                    'bbox': {
                        'type': 'array',
                        'items': {'type': 'number'},
                        'description': 'Bounding box [min_lon, min_lat, max_lon, max_lat] to search in (alternative to lat/lon)',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'control_layers',
            'description': 'Control map layer visibility. Use this when user asks to show/hide/toggle layers like fires, burns, routes, fire stations, or custom analysis results.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'action': {
                        'type': 'string',
                        'enum': ['show', 'hide', 'toggle'],
                        'description': 'Action to perform on layers',
                    },
                    'layers': {
                        'type': 'array',
                        'items': {
                            'type': 'string',
                            'enum': ['fires', 'burns', 'routes', 'fire_stations', 'custom'],
                        },
                        'description': 'List of layer IDs to control. Available: fires (fire hotspots), burns (burned areas), routes (driving routes), fire_stations (fire stations), custom (analysis results/polygons)',
                    },
                },
                'required': ['action', 'layers'],
            },
        },
    },
]

SYSTEM_PROMPT = """You are a wildfire monitoring AI assistant. You help users track active fires,
analyze burn severity, and understand fire danger. You have access to satellite data from VIIRS,
MODIS, and Sentinel-2.

CRITICAL CONSTRAINT: Burn severity mapping (map_burn_area) has a bbox limit of 30° × 30° (approx 3000km × 3000km).
For large regions like "Siberia", "Russia", or entire countries:
- DO NOT use the full region bbox for map_burn_area
- Instead, either:
  1. Ask the user to specify a smaller area (e.g., "Which part of Siberia? Please provide coordinates or a specific region")
  2. Use a representative 30° × 30° bbox centered on the region if the user doesn't specify
  3. For search_fires, you can use larger bboxes (up to 30° × 30°)

When the user asks about fires in a region:
1. Use search_fires to find active hotspots (can use larger bbox)
2. Summarize the results: number of fires, confidence levels, brightness
3. If the user asks about burn area, explain the bbox limitation and ask for a specific area OR use a centered 30° × 30° bbox

When the user asks about burn severity or burned areas:
1. Check if the requested region fits within 30° × 30°
2. If too large, ask for clarification or use a centered bbox
3. Use map_burn_area with appropriate bbox (max 30° × 30°) and dates
4. Summarize the results: total burned area, severity breakdown
5. The map will display burn polygons with severity colors

Always provide context about data sources and limitations.
Fire hotspot locations are approximate (375m for VIIRS, 1km for MODIS).
Burn severity analysis uses Sentinel-2 imagery at 10-20m resolution.

SANDBOX INSTRUCTIONS (execute_python):
1. You have access to an ultra-hardened Python sandbox for custom geospatial tasks.
2. Available libraries (ALREADY IMPORTED, DO NOT use 'import' or 'from ... import'):
   - numpy (as np)
   - pandas (as pd)
   - geopandas (as gpd) — read_file, sjoin, overlay, to_crs and other vector ops
   - shapely.geometry: Point, LineString, Polygon, MultiPoint, MultiLineString, MultiPolygon, LinearRing, GeometryCollection, shape, mapping, box
   - shapely.ops: unary_union, transform, split, nearest_points
   - shapely: wkt, wkb
   - pyproj: CRS, Transformer, Geod, Proj — coordinate transformations and geodesic distances
   - rasterio (raster_open alias for rasterio.open), Resampling, geometry_mask, rasterize, calculate_default_crs, transform_bounds
   - GDAL Python bindings: gdal, ogr, osr (from osgeo) — any GDAL-supported format (GeoTIFF, COG, Shapefile, GeoJSON, GPKG, KML, NetCDF, HDF5, JPEG2000...)
   - xarray (as xr) + rioxarray — labeled n-D arrays, raster clipping/warping/reprojection
   - mercantile — tile math (XYZ tiles, bbox -> tiles)
3. Projection tips: reproject vectors with gdf.to_crs("EPSG:3857") or Transformer.from_crs(CRS.from_epsg(4326), CRS.from_epsg(3857), always_xy=True). Use Geod(ellps="WGS84").geometry_area_perimeter() for accurate areas in m2 instead of degree-based .area/.buffer. For buffers in meters, first reproject to an equal-area/local CRS (e.g. UTM zone via CRS.from_epsg).
4. CRITICAL: DO NOT write 'import' or 'from ... import' statements — all libraries are pre-imported! Use classes directly: LineString([...]), Point(...), Polygon(...), etc.
5. NO network access is allowed. You cannot use requests, httpx, or fetch URLs. Remote rasters (/vsicurl/, https://) are unavailable; work with data passed via context.
6. To pass data from previous tools, use the context parameter (max 10MB).
7. To display results on map, assign GeoJSON to __result__ variable (must be WGS84 / EPSG:4326).
8. Available builtins: round, abs, min, max, sum, sorted, enumerate, zip, map, filter, len, int, float, str, list, dict, tuple, set, print, and all standard exceptions.

BUFFER EXAMPLES (use shapely + geopandas, NOT manual cos/sin calculations):

User: "Create 20km buffer around a point"
You:
1. Call execute_python with code:
   # Create point
   point = Point(20.51, 54.71)
   gdf = gpd.GeoDataFrame({'geometry': [point]}, crs='EPSG:4326')
   # Reproject to UTM for metric buffer
   gdf_utm = gdf.to_crs('EPSG:32634')
   gdf_utm['geometry'] = gdf_utm.geometry.buffer(20000)  # 20km in meters
   # Reproject back to WGS84
   gdf_wgs = gdf_utm.to_crs('EPSG:4326')
   __result__ = gdf_wgs.__geo_interface__

User: "Create 30km buffer around a line"
You:
1. Call execute_python with code:
   # Create line from coordinates
   line = LineString([(20.15, 54.93), (20.51, 54.71), (21.82, 54.63)])
   gdf = gpd.GeoDataFrame({'geometry': [line]}, crs='EPSG:4326')
   # Reproject to UTM for metric buffer
   gdf_utm = gdf.to_crs('EPSG:32634')
   gdf_utm['geometry'] = gdf_utm.geometry.buffer(30000)  # 30km in meters
   # Reproject back to WGS84
   gdf_wgs = gdf_utm.to_crs('EPSG:4326')
   __result__ = gdf_wgs.__geo_interface__

CRITICAL: NEVER manually calculate cos/sin for buffers! Always use gdf.to_crs() + gdf.geometry.buffer(meters) + gdf.to_crs('EPSG:4326')

MULTI-STEP WORKFLOWS:
When user asks to "find fires AND filter/buffer/analyze them":
1. First call search_fires to get fire data
2. Then call execute_python — the system AUTOMATICALLY passes previous tool results in context
3. In your code, access fires via context['search_fires']
4. Process and return __result__ for map display

Example workflows:

User: "Find fires and show only those with FRP > 20 MW"
You:
1. Call search_fires(bbox=[...], days=10)
2. Call execute_python with code:
   fires_geojson = context['search_fires']
   filtered = [f for f in fires_geojson['features'] if f['properties'].get('frp', 0) > 20]
   __result__ = {"type": "FeatureCollection", "features": filtered}

User: "Create 50km buffer around high confidence fires"
You:
1. Call search_fires(bbox=[...], min_confidence='high')
2. Call execute_python with code:
   fires = context['search_fires']
   gdf = gpd.GeoDataFrame.from_features(fires['features'], crs='EPSG:4326')
   # Buffer ~0.45 degrees (approx 50km at mid-latitudes)
   gdf['geometry'] = gdf.geometry.buffer(0.45)
   __result__ = gdf.__geo_interface__

User: "Create grid of points in bbox"
You:
1. Call execute_python with code:
   lons = np.linspace(bbox[0], bbox[2], 5)
   lats = np.linspace(bbox[1], bbox[3], 5)
   features = []
   for lon in lons:
       for lat in lats:
           features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]}, "properties": {}})
   __result__ = {"type": "FeatureCollection", "features": features}

ROUTING INSTRUCTIONS (build_route, build_routes_batch) — CRITICAL:
1. ALWAYS use build_route or build_routes_batch for driving routes. NEVER draw LineString in execute_python for routes!
2. These tools use OSRM to calculate REAL road paths, not straight lines.
3. For SINGLE route: use build_route(start=[lon, lat], end=[lon, lat])
4. For MULTIPLE routes: use build_routes_batch(route_pairs=[[[start1_lon, start1_lat], [end1_lon, end1_lat]], ...])
5. build_routes_batch is more efficient for multiple routes (one call instead of many).

FORBIDDEN: Do NOT create routes with execute_python like:
   ❌ LineString([station_coords, fire_coords])  # This is a STRAIGHT LINE, not a road route!
   ❌ gpd.GeoDataFrame with LineString geometry for routes

REQUIRED: Always use routing tools:
   ✅ build_route(start=[37.6, 55.7], end=[37.9, 55.8])  # Single route
   ✅ build_routes_batch(route_pairs=[[[37.6, 55.7], [37.9, 55.8]], [[37.5, 55.6], [37.8, 55.9]]])  # Multiple routes

Example workflows:

User: "Build route from fire station to fire"
You:
1. Call find_nearest_fire_stations(lat=fire_lat, lon=fire_lon, limit=1)
2. Get station coordinates from result
3. Call build_route(start=[station_lon, station_lat], end=[fire_lon, fire_lat])

User: "Find fires and nearest stations"
You:
1. Call search_fires to get fire locations
2. For each fire, call find_nearest_fire_stations(lat=fire_lat, lon=fire_lon, limit=1)
3. Display only the ONE nearest station for each fire

User: "Find fires near a city AND build routes from fire stations to them" (ПОЛНЫЙ WORKFLOW):
1. Call search_fires(bbox=[...]) — get fire hotspots
2. Call dispatch_routes_to_fires() WITHOUT arguments — the system automatically injects
   the previous search_fires GeoJSON; it clusters fires, finds the nearest station for
   each cluster (Overpass) and builds driving routes (OSRM) in one step. Routes and
   stations appear on the map. NEVER try to copy fire/station coordinates into
   execute_python or extract them yourself — this always fails and wastes iterations!
3. Summarize using the 'routes' list returned by dispatch_routes_to_fires
   (it contains exact [lon, lat] of each station and fire).
Alternative if you already have explicit station+fire coordinates:
   build_routes_batch(route_pairs=[[[st_lon, st_lat], [fire_lon, fire_lat]], ...])

ROUTE LENGTH LABELS (label_route_lengths) — CRITICAL:
When the user asks to label/annotate route lengths ("подпиши длину маршрутов",
"покажи расстояние на маршрутах"):
1. Call label_route_lengths() WITHOUT arguments — the system automatically injects
   the GeoJSON of routes built earlier in this conversation. Labels appear at the
   midpoint of each route instantly.
2. NEVER use execute_python for this task and NEVER recompute routes there — it is
   extremely slow, requires copying coordinates by hand and often produces nothing.
3. If routes were built in a PREVIOUS turn (you only see them in [Контекст: ...]),
   call dispatch_routes_to_fires or build_routes_batch again first (results are cached,
   so it returns fast), then call label_route_lengths().

POPUPS AND LABELS ON MARKERS:
- Popup text on click goes into properties.popup (HTML allowed, e.g. "<b>Сочи</b><br>43.58° с.ш.").
- Permanent map labels go into properties.label (plain text; '\n' makes multi-line labels).
- Markers returned from execute_python MUST be Point features inside a FeatureCollection
  assigned to __result__ — otherwise nothing appears on the map.

IMPORTANT: Do NOT build routes unless user explicitly asks for routes!

FIRE STATION INSTRUCTIONS (find_nearest_fire_stations):
1. Use find_nearest_fire_stations to locate nearby fire stations via OpenStreetMap.
2. Parameters: lat, lon (search center), radius_km (default: 50), limit (max stations to return)
3. IMPORTANT: Always use limit=1 when finding nearest station for each fire point!
4. Returns: GeoJSON with fire station points, distances, and contact info
5. Example workflow for "find nearest station to each fire":
   a. Call search_fires to get fire locations
   b. For EACH fire, call find_nearest_fire_stations(lat=fire_lat, lon=fire_lon, limit=1)
   c. This returns ONLY the ONE nearest station for that fire

LAYER CONTROL INSTRUCTIONS (control_layers):
1. Use control_layers when user asks to show/hide/toggle map layers.
2. Available layers: fires, burns, routes, fire_stations, custom
3. Actions: show (make visible), hide (make invisible), toggle (switch state)
4. Examples:
   - User: "убери лиловый полигон" → control_layers(action="hide", layers=["custom"])
   - User: "покажи маршруты" → control_layers(action="show", layers=["routes"])
   - User: "скрой пожары" → control_layers(action="hide", layers=["fires"])
   - User: "переключи гари" → control_layers(action="toggle", layers=["burns"])
5. Layer descriptions:
   - fires: fire hotspots (red/orange/green points)
   - burns: burned areas (red/orange/yellow polygons)
   - routes: driving routes (orange dashed lines)
   - fire_stations: fire stations (blue points)
   - custom: analysis results from sandbox (purple polygons/points)

MULTI-TURN CONVERSATION CONTEXT:
When the user refers to previous results (e.g., "построй маршруты к тем пожарам", "проанализируй эти гари"),
check the conversation history for context summaries marked with [Контекст: ...].
These summaries contain key data like fire coordinates, route details, and other results from previous tool calls.
Use these coordinates to perform follow-up actions without asking the user to repeat the request.

Example:
1. User: "найди пожары в Тульской области"
2. Assistant calls search_fires and responds with text + "[Контекст: найдено 5 пожаров. Координаты первых 5: [37.6, 54.1], [37.8, 54.2], ...]"
3. User: "построй маршруты от пожарных частей к этим пожарам"
4. Assistant sees the context in history, extracts coordinates, and calls dispatch_routes_to_fires or build_routes_batch

IMPORTANT: When user says "к этим пожарам", "к найденным пожарам", "от этих частей" — use the coordinates from [Контекст: ...] in the previous assistant message.

Respond in the same language as the user's message."""


MAX_HISTORY_MESSAGES = 20


def _build_messages(message, bbox=None, history=None):
    """
    Assemble the LLM message list: system prompt + conversation history + current user message.

    History is a list of {'role': 'user'|'assistant', 'content': str} entries from previous turns.
    Only the last MAX_HISTORY_MESSAGES valid entries are kept; system/error messages are skipped.
    """
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]

    if history:
        kept = 0
        for item in reversed(history):
            if kept >= MAX_HISTORY_MESSAGES:
                break
            if not isinstance(item, dict):
                continue
            role = item.get('role')
            content = item.get('content')
            if role not in ('user', 'assistant') or not isinstance(content, str) or not content.strip():
                continue
            messages.insert(1, {'role': role, 'content': content})
            kept += 1

    user_content = message
    if bbox:
        user_content += f'\n\n[User is viewing area: bbox={bbox}]'
    messages.append({'role': 'user', 'content': user_content})
    return messages


def handle_chat_message(message, bbox=None, history=None):
    """
    Process a chat message and return AI response with optional map data.

    Args:
        message: User message text
        bbox: Optional bounding box [minx, miny, maxx, maxy]
        history: Optional list of prior conversation turns
                 [{'role': 'user'|'assistant', 'content': str}, ...]

    Returns:
        Dict with keys:
        - response: AI text response
        - map_data: Optional GeoJSON data for map display
        - actions: List of actions taken
        - layer_actions: Optional list of layer control actions
    """
    client = OpenAI(
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_API_BASE_URL,
        timeout=float(getattr(settings, 'LLM_REQUEST_TIMEOUT', 90)),
        max_retries=1,
    )

    messages = _build_messages(message, bbox, history)

    actions = []
    map_data_list = []
    tool_results = {}
    layer_actions = []  # Команды управления слоями
    max_iterations = 10  # Защита от бесконечного цикла
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        
        try:
            response = client.chat.completions.create(
                model=settings.LLM_MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice='auto',
            )
        except Exception as e:
            logger.error(f'LLM API call failed: {e}')
            return {
                'response': f'Извините, произошла ошибка при обработке запроса: {e}',
                'map_data': None,
                'actions': actions,
            }

        assistant_message = response.choices[0].message
        
        # Если нет tool_calls — это финальный ответ
        if not assistant_message.tool_calls:
            text = assistant_message.content or ''
            if not text.strip():
                text = _format_tool_results(actions)
            break
        
        # Обработка tool calls
        messages.append(assistant_message)

        for tool_call in assistant_message.tool_calls:
            func_name = tool_call.function.name
            func_args = json.loads(tool_call.function.arguments)

            # Если это execute_python и нет явного context — добавляем результаты предыдущих инструментов
            if func_name == 'execute_python' and not func_args.get('context') and tool_results:
                func_args['context'] = tool_results

            # dispatch_routes_to_fires: автоматически подставляем GeoJSON пожаров
            # из предыдущего вызова search_fires (LLM не должен копировать данные сам)
            if func_name == 'dispatch_routes_to_fires':
                if not func_args.get('fires_geojson'):
                    fires_gj = tool_results.get('search_fires') or _recall_tool_data(
                        'fires_geojson', 'search_fires')
                    if isinstance(fires_gj, dict) and fires_gj.get('features'):
                        func_args['fires_geojson'] = fires_gj

            # label_route_lengths: автоматически подставляем GeoJSON ранее
            # построенных маршрутов (LLM не должен копировать геометрию вручную)
            if func_name == 'label_route_lengths' and not func_args.get('routes_geojson'):
                routes_gj = tool_results.get('dispatch_routes_to_fires') \
                    or tool_results.get('build_routes_batch') \
                    or tool_results.get('build_route') \
                    or _recall_tool_data(
                        'routes_geojson', 'dispatch_routes_to_fires',
                        'build_routes_batch', 'build_route')
                if isinstance(routes_gj, dict) and routes_gj.get('features'):
                    func_args['routes_geojson'] = routes_gj

            logger.info(
                f'[chat] iteration={iteration} tool={func_name} args={_args_for_log(func_name, func_args)}'
            )
            result = execute_tool(func_name, func_args, bbox)
            _remember_tool_data(func_name, result)
            actions.append({
                'tool': func_name,
                'args': _args_for_log(func_name, func_args),
                'result_summary': result.get('summary', ''),
            })

            # Сохраняем результат для передачи в следующие инструменты
            if result.get('geojson'):
                tool_results[func_name] = result['geojson']
                map_data_list.append({
                    'data': result['geojson'],
                    'type': result.get('data_type', 'fire'),
                })
            elif result.get('map_data'):
                tool_results[func_name] = result['map_data'].get('data')
                map_data_list.append(result['map_data'])

            # Собираем layer_actions
            if result.get('layer_actions'):
                layer_actions.extend(result['layer_actions'])

            messages.append({
                'role': 'tool',
                'tool_call_id': tool_call.id,
                'content': json.dumps(
                    result.get('summary', {}), ensure_ascii=False, default=str
                ),
            })
    else:
        # Превышен лимит итераций
        text = f"Превышен лимит итераций ({max_iterations}). Выполнено действий: {len(actions)}"

    logger.info(
        f'[chat] done: {len(actions)} action(s): '
        + ', '.join(a['tool'] for a in actions)
    )

    # Add context summary to response for multi-turn conversation support
    # This ensures key data (coordinates, routes) is preserved in history
    context_summary = _build_context_summary(actions, tool_results)
    if context_summary and text:
        text = text + '\n\n' + context_summary

    return {
        'response': text,
        'map_data_list': map_data_list,  # Все результаты с типами
        'actions': actions,
        'layer_actions': layer_actions if layer_actions else None,
    }


def execute_tool(name, args, context_bbox=None):
    """Execute a tool and return results."""
    bbox = args.get('bbox') or context_bbox

    if name == 'search_fires':
        source = args.get('source', 'all')
        days = args.get('days', 1)

        if source == 'all':
            from .firms_client import fetch_all_sources
            fires = fetch_all_sources(bbox=bbox, days=days)
        else:
            fires = fetch_active_fires(source=source, bbox=bbox, days=days)

        valid, filtered = filter_fire_hotspots(fires)

        geojson = _fires_to_geojson(valid)
        summary = get_filter_summary(valid, filtered)

        return {
            'geojson': geojson,
            'summary': {
                'total_fires': len(valid),
                'filtered_out': len(filtered),
                'filter_summary': summary,
                'sample': [
                    {
                        'lat': f['latitude'],
                        'lon': f['longitude'],
                        'brightness': f['brightness'],
                        'confidence': f['confidence'],
                        'frp': f.get('frp'),
                        'source': f['source'],
                    }
                    for f in valid[:10]
                ],
            },
        }

    elif name == 'search_sentinel2_scenes':
        scenes = search_sentinel2(
            bbox=bbox,
            start_date=args.get('start_date'),
            end_date=args.get('end_date'),
            max_cloud_cover=args.get('max_cloud_cover', 30),
        )

        return {
            'summary': {
                'scenes_found': len(scenes),
                'scenes': [
                    {
                        'id': s.id,
                        'datetime': str(s.datetime),
                        'cloud_cover': s.properties.get('eo:cloud_cover'),
                    }
                    for s in scenes[:10]
                ],
            },
        }

    elif name == 'get_fire_statistics':
        days = args.get('days', 7)
        from .firms_client import fetch_all_sources
        fires = fetch_all_sources(bbox=bbox, days=days)
        valid, filtered = filter_fire_hotspots(fires)

        by_source = {}
        by_confidence = {}
        for f in valid:
            src = f['source']
            conf = f['confidence']
            by_source[src] = by_source.get(src, 0) + 1
            by_confidence[conf] = by_confidence.get(conf, 0) + 1

        return {
            'summary': {
                'total_valid': len(valid),
                'total_filtered': len(filtered),
                'by_source': by_source,
                'by_confidence': by_confidence,
                'days': days,
            },
        }

    elif name == 'map_burn_area':
        from .stac_client import get_sentinel2_assets, get_scl_asset
        from .burn_severity import process_sentinel2_burn
        from fires.models import BurnArea

        pre_date = args.get('pre_date')
        post_date = args.get('post_date')
        max_cloud_cover = args.get('max_cloud_cover', 30)

        if not pre_date or not post_date:
            return {'summary': {'error': 'pre_date and post_date are required'}}

        try:
            # Search for pre-fire Sentinel-2 scenes (search in range: pre_date - 15 days to pre_date)
            from datetime import timedelta
            pre_date_obj = datetime.strptime(pre_date, '%Y-%m-%d').date()
            pre_date_start = (pre_date_obj - timedelta(days=15)).isoformat()
            
            pre_scenes = search_sentinel2(
                bbox=bbox,
                start_date=pre_date_start,
                end_date=pre_date,
                max_cloud_cover=max_cloud_cover,
                limit=5,
            )

            if not pre_scenes:
                return {'summary': {'error': f'No pre-fire Sentinel-2 scenes found for {pre_date_start} to {pre_date}'}}

            # Select best pre-fire scene (lowest cloud cover)
            pre_scene = min(pre_scenes, key=lambda s: s.properties.get('eo:cloud_cover', 100))

            # Search for post-fire Sentinel-2 scenes (search in range: post_date to post_date + 15 days)
            post_date_obj = datetime.strptime(post_date, '%Y-%m-%d').date()
            post_date_end = (post_date_obj + timedelta(days=15)).isoformat()
            
            post_scenes = search_sentinel2(
                bbox=bbox,
                start_date=post_date,
                end_date=post_date_end,
                max_cloud_cover=max_cloud_cover,
                limit=5,
            )

            if not post_scenes:
                return {'summary': {'error': f'No post-fire Sentinel-2 scenes found for {post_date} to {post_date_end}'}}

            # Select best post-fire scene (lowest cloud cover)
            post_scene = min(post_scenes, key=lambda s: s.properties.get('eo:cloud_cover', 100))

            # Get signed asset URLs
            pre_assets = get_sentinel2_assets(pre_scene, bands=['B08', 'B12'])
            post_assets = get_sentinel2_assets(post_scene, bands=['B08', 'B12'])

            # Get SCL assets for cloud masking
            pre_scl_url = get_scl_asset(pre_scene)
            post_scl_url = get_scl_asset(post_scene)

            # Process burn severity with cloud masking
            result = process_sentinel2_burn(
                pre_assets=pre_assets,
                post_assets=post_assets,
                bbox=bbox,
                n_classes=4,
                pre_scl_url=pre_scl_url,
                post_scl_url=post_scl_url,
                apply_cloud_masking=True,
            )

            stats = result['stats']
            severity_data = result['severity']
            transform = result['transform']
            source_crs = result['crs']

            # Vectorize and save burn polygons to database
            from django.contrib.gis.geos import Polygon as DjangoPolygon, GEOSGeometry
            from rasterio.features import shapes
            from shapely.geometry import shape
            from shapely.ops import unary_union
            import numpy as np

            # Create transformer for reprojection to WGS84
            transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
            project = lambda x, y: transformer.transform(x, y)

            # Optimization parameters
            min_area_ha = 1.0
            simplify_tolerance = 0.005
            downsample_factor = 4

            # Calculate area conversion factor
            avg_lat = (bbox[1] + bbox[3]) / 2
            lat_rad = np.radians(avg_lat)
            deg_to_m_lat = 111000
            deg_to_m_lon = 111000 * np.cos(lat_rad)
            deg2_to_ha = (deg_to_m_lat * deg_to_m_lon) / 10000

            features = []
            for severity_class in ['low', 'moderate', 'high']:
                if severity_class not in stats or stats[severity_class]['area_ha'] == 0:
                    continue

                # Create mask and downsample
                mask = (severity_data == severity_class).astype(np.uint8)
                if downsample_factor > 1:
                    mask_downsampled = mask[::downsample_factor, ::downsample_factor]
                    downsampled_transform = transform * transform.scale(downsample_factor, downsample_factor)
                else:
                    mask_downsampled = mask
                    downsampled_transform = transform

                # Vectorize
                polygons = []
                for geom, value in shapes(mask_downsampled.astype(np.uint8), transform=downsampled_transform):
                    if value == 1:
                        try:
                            poly = shape(geom)
                            if poly.is_valid and poly.area > 0:
                                area_ha = poly.area * deg2_to_ha
                                if area_ha >= min_area_ha:
                                    polygons.append(poly)
                        except Exception:
                            continue

                if not polygons:
                    continue

                # Merge and simplify
                merged = unary_union(polygons)
                merged = merged.simplify(simplify_tolerance, preserve_topology=True)

                # Reproject from UTM to WGS84
                merged_wgs84 = shapely_transform(project, merged)

                # Save to database
                try:
                    if merged_wgs84.geom_type == 'Polygon':
                        geos_geom = GEOSGeometry(merged_wgs84.wkt, srid=4326)
                    elif merged_wgs84.geom_type == 'MultiPolygon':
                        geos_geom = GEOSGeometry(merged_wgs84.wkt, srid=4326)
                    else:
                        continue

                    burn_area = BurnArea.objects.create(
                        geometry=geos_geom,
                        area_ha=stats[severity_class]['area_ha'],
                        severity=severity_class,
                        dnbr_mean=stats[severity_class]['mean_dnbr'],
                        pre_date=pre_date,
                        post_date=post_date,
                        satellite='Sentinel-2',
                        tile_id=pre_scene.id,
                    )

                    features.append({
                        'type': 'Feature',
                        'geometry': json.loads(burn_area.geometry.geojson),
                        'properties': {
                            'severity': burn_area.severity,
                            'area_ha': burn_area.area_ha,
                            'mean_dnbr': burn_area.dnbr_mean,
                            'pre_date': str(burn_area.pre_date),
                            'post_date': str(burn_area.post_date),
                        },
                    })
                except Exception as e:
                    logger.error(f'Failed to save burn area: {e}')
                    continue

            return {
                'geojson': {
                    'type': 'FeatureCollection',
                    'features': features,
                },
                'data_type': 'burn',
                'summary': {
                    'total_burned_ha': stats['total_burned_ha'],
                    'by_severity': {
                        k: {'area_ha': v['area_ha'], 'mean_dnbr': v['mean_dnbr']}
                        for k, v in stats.items()
                        if k != 'total_burned_ha'
                    },
                    'pre_scene': pre_scene.id,
                    'post_scene': post_scene.id,
                    'pre_date': pre_date,
                    'post_date': post_date,
                    'polygons_loaded': len(features),
                },
            }

        except Exception as e:
            logger.exception('Burn mapping failed')
            return {'summary': {'error': f'Burn mapping failed: {str(e)}'}}

    elif name == 'generate_burn_report':
        from .report_generator import BurnReportGenerator

        date_from = args.get('date_from')
        date_to = args.get('date_to')

        if not date_from or not date_to:
            return {'summary': {'error': 'date_from and date_to are required'}}

        try:
            generator = BurnReportGenerator()
            report_data = generator.generate_report(
                bbox=bbox,
                date_from=date_from,
                date_to=date_to,
                include_landcover=True,
            )

            if 'error' in report_data:
                return {'summary': {'error': report_data['error']}}

            # Format summary for LLM
            summary = report_data['summary']
            severity_dist = report_data['severity_distribution']

            return {
                'summary': {
                    'report_id': report_data['report_id'],
                    'generated_at': report_data['generated_at'],
                    'total_burn_area_ha': summary['total_burn_area_ha'],
                    'burn_polygons_count': summary['burn_polygons_count'],
                    'severity_distribution': severity_dist,
                    'data_quality': report_data['data_quality'],
                },
            }

        except Exception as e:
            logger.exception('Report generation failed')
            return {'summary': {'error': f'Report generation failed: {str(e)}'}}

    elif name == 'execute_python':
        import httpx
        import os
        
        code = args.get('code')
        context = args.get('context', {})
        
        # Лимит на размер context (10MB)
        MAX_CONTEXT_SIZE_MB = 10
        try:
            context_json = json.dumps(context)
            if len(context_json.encode('utf-8')) > MAX_CONTEXT_SIZE_MB * 1024 * 1024:
                return {"summary": {"error": f"Context too large (>{MAX_CONTEXT_SIZE_MB}MB)"}}
        except (TypeError, ValueError):
            return {"summary": {"error": "Context contains non-serializable data"}}

        sandbox_url = os.getenv("SANDBOX_URL", "http://sandbox:8002/execute")

        try:
            # Используем общий клиент из routing.py для connection pooling
            from .routing import _get_http_client
            client = _get_http_client()
            response = client.post(
                sandbox_url,
                json={'code': code, 'context': context}
            )

            if response.status_code == 400:
                return {"summary": {"error": f"Security violation: {response.json().get('detail')}"}}
            if response.status_code == 408:
                return {"summary": {"error": "Timeout: Code took too long to execute (>30s)."}}
            if response.status_code == 429:
                return {"summary": {"error": "Rate limit exceeded. Try again later."}}
            if response.status_code != 200:
                return {"summary": {"error": f"Sandbox error: {response.text}"}}

            result = response.json()

            summary = {
                "success": result["success"],
                "stdout": result["stdout"][:500] if result["stdout"] else "",
                "stderr": result["stderr"][:500] if result["stderr"] else "",
            }

            response_payload = {"summary": summary}

            # Если код вернул GeoJSON, передаем на фронтенд
            if result.get("result"):
                response_payload["map_data"] = {
                    "type": "custom",
                    "data": result["result"]
                }

            return response_payload

        except httpx.ConnectError:
            return {"summary": {"error": "Sandbox service is unavailable. Is it running?"}}
        except Exception as e:
            return {"summary": {"error": f"Failed to connect to sandbox: {str(e)}"}}

    elif name == 'build_route':
        start = args.get('start')
        end = args.get('end')

        if not start or not end:
            return {"summary": {"error": "start and end points are required"}}

        # Используем общую функцию из routing.py
        result = build_route(start, end)
        
        if not result.get('success'):
            return {"summary": {"error": result.get('error', 'Unknown error')}}

        return {
            "summary": {
                "distance_km": result['distance_km'],
                "duration_min": result['duration_min'],
                "success": True
            },
            # ВАЖНО: ключ geojson — цикл handle_chat_message кладёт такой результат
            # в tool_results, чтобы LLM видел координаты маршрута в контексте
            "geojson": result['geojson'],
            "data_type": "routes",
        }

    elif name == 'build_routes_batch':
        route_pairs = args.get('route_pairs')

        if not route_pairs or not isinstance(route_pairs, list):
            return {"summary": {"error": "route_pairs is required and must be a list"}}

        # Используем общую функцию из routing.py
        result = build_routes_batch(route_pairs)

        if not result.get('success'):
            return {"summary": {"error": "No routes were built", "details": result.get('errors')}}

        return {
            "summary": {
                "total_routes": result['total_routes'],
                "successful_routes": result['successful_routes'],
                "total_distance_km": result['total_distance_km'],
                "total_duration_min": result['total_duration_min'],
                "success": True
            },
            "geojson": result['geojson'],
            "data_type": "routes",
        }

    elif name == 'find_nearest_fire_stations':
        lat = args.get('lat')
        lon = args.get('lon')
        radius_km = args.get('radius_km', 50)
        limit = args.get('limit')  # None = вернуть все найденные
        bbox = args.get('bbox')  # [min_lon, min_lat, max_lon, max_lat]

        # Используем общую функцию из routing.py
        result = find_nearest_fire_stations(lat, lon, radius_km, limit, bbox)

        if not result.get('success'):
            return {"summary": {"error": result.get('error', 'Unknown error')}}

        summary = {
            "total_found": result['total_found'],
            "returned_count": result['returned_count'],
            "search_mode": result['search_mode'],
            "success": True
        }
        
        # Добавляем информацию о поиске
        if result['search_mode'] == 'bbox':
            summary['message'] = f"Найдено {result['total_found']} пожарных частей в экстенте карты"
        else:
            summary['message'] = f"Найдено {result['total_found']} пожарных частей, показано {result['returned_count']}"

        return {
            "summary": summary,
            # Ключ geojson: результат попадёт в tool_results и LLM увидит
            # координаты станций ([lon, lat]) для построения маршрутов
            "geojson": result['geojson'],
            "data_type": "fire_stations",
        }

    elif name == 'dispatch_routes_to_fires':
        return _dispatch_routes_to_fires(args)

    elif name == 'label_route_lengths':
        import copy as _copy

        gj = args.get('routes_geojson')
        if not isinstance(gj, dict) or not gj.get('features'):
            gj = _recall_tool_data(
                'routes_geojson', 'dispatch_routes_to_fires',
                'build_routes_batch', 'build_route',
            )
        if not isinstance(gj, dict) or not gj.get('features'):
            return {'summary': {'error': (
                'No routes found. Build routes first with '
                'dispatch_routes_to_fires (or build_routes_batch), '
                'then call label_route_lengths again.'
            )}}

        labeled = add_route_length_labels(_copy.deepcopy(gj))
        labels_added = sum(
            1 for f in labeled.get('features', [])
            if isinstance(f, dict)
            and (f.get('properties') or {}).get('type') == 'route_label'
        )
        lines = sum(
            1 for f in labeled.get('features', [])
            if isinstance(f, dict)
            and (f.get('geometry') or {}).get('type') == 'LineString'
        )
        summary = {
            'success': True,
            'labels_added': labels_added,
            'routes_found': lines,
            'message': (
                f'Длины подписаны на карте: {labels_added} шт. для {lines} '
                'маршрутов (подписи стоят посередине каждого маршрута).'
            ),
        }
        return {
            'summary': summary,
            'geojson': labeled,
            'data_type': 'routes',
        }

    elif name == 'control_layers':
        action_type = args.get('action')
        layers = args.get('layers', [])

        if not action_type or not layers:
            return {"summary": {"error": "action and layers are required"}}

        # Формируем layer_actions для фронтенда
        layer_actions = []
        for layer_id in layers:
            layer_actions.append({
                "action": action_type,
                "layer": layer_id
            })

        layer_names = {
            'fires': 'Очаги пожаров',
            'burns': 'Гари',
            'routes': 'Маршруты',
            'fire_stations': 'Пожарные части',
            'custom': 'Результаты анализа'
        }

        action_names = {
            'show': 'показан',
            'hide': 'скрыт',
            'toggle': 'переключен'
        }

        layers_display = ', '.join([layer_names.get(l, l) for l in layers])
        action_display = action_names.get(action_type, action_type)

        return {
            "summary": {
                "success": True,
                "message": f"Слой '{layers_display}' {action_display}"
            },
            "layer_actions": layer_actions
        }

    return {'summary': {'error': f'Unknown tool: {name}'}}


def _fires_to_geojson(fires):
    """Convert fire list to GeoJSON FeatureCollection."""
    features = []
    for f in fires:
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [f['longitude'], f['latitude']],
            },
            'properties': {
                'brightness': f['brightness'],
                'confidence': f['confidence'],
                'frp': f.get('frp'),
                'source': f['source'],
                'detected_at': f['detected_at'].isoformat() if isinstance(f['detected_at'], datetime) else str(f['detected_at']),
                'daynight': f.get('daynight', 'D'),
            },
        })

    return {
        'type': 'FeatureCollection',
        'features': features,
    }


def _format_tool_results(actions):
    """Format tool results as text when follow-up LLM call fails."""
    parts = []
    for action in actions:
        tool = action['tool']
        summary = action['result_summary']
        if tool == 'search_fires':
            total = summary.get('total_fires', 0)
            parts.append(f'Найдено {total} активных очагов пожара.')
        elif tool == 'search_sentinel2_scenes':
            count = summary.get('scenes_found', 0)
            parts.append(f'Найдено {count} сцен Sentinel-2.')
        elif tool == 'get_fire_statistics':
            total = summary.get('total_valid', 0)
            parts.append(f'Статистика: {total} подтверждённых очагов.')
    return '\n'.join(parts) if parts else 'Данные получены.'


def _build_context_summary(actions, tool_results):
    """
    Build a context summary from tool results to include in the assistant's response.
    This ensures that key data (coordinates, route info) is preserved in conversation
    history and available for follow-up questions.
    """
    if not actions:
        return ''

    context_parts = []

    for action in actions:
        tool = action['tool']
        summary = action.get('result_summary', {})

        if tool == 'search_fires':
            # Include fire coordinates for follow-up routing/analysis
            sample = summary.get('sample', [])
            if sample:
                coords = [f"[{f['lon']:.4f}, {f['lat']:.4f}]" for f in sample[:5]]
                total = summary.get('total_fires', 0)
                context_parts.append(
                    f"[Контекст: найдено {total} пожаров. Координаты первых 5: {', '.join(coords)}]"
                )

        elif tool == 'dispatch_routes_to_fires':
            # Include route details for follow-up questions
            routes = summary.get('routes', [])
            if routes:
                route_info = []
                for r in routes[:3]:
                    station = r.get('station', [])
                    fire = r.get('fire', [])
                    if station and fire:
                        route_info.append(
                            f"от [{station[0]:.4f}, {station[1]:.4f}] к [{fire[0]:.4f}, {fire[1]:.4f}]"
                        )
                total_routes = summary.get('routes_built', 0)
                context_parts.append(
                    f"[Контекст: построено {total_routes} маршрутов. "
                    f"Примеры: {'; '.join(route_info)}]"
                )

        elif tool == 'build_route':
            # Include single route details
            start = summary.get('start')
            end = summary.get('end')
            distance = summary.get('distance_km')
            if start and end:
                context_parts.append(
                    f"[Контекст: маршрут от [{start[0]:.4f}, {start[1]:.4f}] "
                    f"до [{end[0]:.4f}, {end[1]:.4f}], {distance:.1f} км]"
                )

        elif tool == 'build_routes_batch':
            # Include batch route summary
            total = summary.get('successful_routes', 0)
            distance = summary.get('total_distance_km', 0)
            context_parts.append(
                f"[Контекст: построено {total} маршрутов, общая дистанция {distance:.1f} км]"
            )

    return '\n'.join(context_parts) if context_parts else ''
