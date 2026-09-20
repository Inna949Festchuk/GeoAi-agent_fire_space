"""
AI chat handler — processes user messages and calls appropriate tools.

Uses OpenAI-compatible API (Qwen3.7-plus via routerai.ru) with function calling
to determine which fire monitoring tools to invoke.
"""

import json
import logging
from datetime import datetime, timedelta
from pyproj import Transformer
from shapely.ops import transform as shapely_transform
from django.conf import settings
from openai import OpenAI

from .firms_client import fetch_active_fires
from .fire_filter import filter_fire_hotspots, get_filter_summary
from .stac_client import search_sentinel2

logger = logging.getLogger(__name__)

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

Respond in the same language as the user's message."""


def handle_chat_message(message, bbox=None):
    """
    Process a chat message and return AI response with optional map data.

    Args:
        message: User message text
        bbox: Optional bounding box [minx, miny, maxx, maxy]

    Returns:
        Dict with keys:
        - response: AI text response
        - map_data: Optional GeoJSON data for map display
        - actions: List of actions taken
    """
    client = OpenAI(
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_API_BASE_URL,
    )

    user_content = message
    if bbox:
        user_content += f'\n\n[User is viewing area: bbox={bbox}]'

    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': user_content},
    ]

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
            'actions': [],
        }

    assistant_message = response.choices[0].message
    actions = []
    map_data_list = []  # Список всех map_data с типами

    if assistant_message.tool_calls:
        messages.append(assistant_message)

        for tool_call in assistant_message.tool_calls:
            func_name = tool_call.function.name
            func_args = json.loads(tool_call.function.arguments)

            result = execute_tool(func_name, func_args, bbox)
            actions.append({
                'tool': func_name,
                'args': func_args,
                'result_summary': result.get('summary', ''),
            })

            if result.get('geojson'):
                map_data_list.append({
                    'data': result['geojson'],
                    'type': result.get('data_type', 'fire'),
                })

            messages.append({
                'role': 'tool',
                'tool_call_id': tool_call.id,
                'content': json.dumps(result.get('summary', {}), ensure_ascii=False),
            })

        try:
            final_response = client.chat.completions.create(
                model=settings.LLM_MODEL,
                messages=messages,
            )
            text = final_response.choices[0].message.content
            # Fallback if LLM returns empty response
            if not text or text.strip() == '':
                text = _format_tool_results(actions)
        except Exception as e:
            logger.error(f'LLM follow-up call failed: {e}')
            text = _format_tool_results(actions)
    else:
        text = assistant_message.content or ''

    return {
        'response': text,
        'map_data_list': map_data_list,  # Все результаты с типами
        'actions': actions,
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
