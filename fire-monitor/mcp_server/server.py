"""
FastMCP server for Fire Monitor.

Provides MCP tools for AI agents to query fire data,
search satellite imagery, get fire statistics, build routes,
and execute custom geospatial code.
"""

import os
import json
import httpx
from fastmcp import FastMCP

BACKEND_URL = os.environ.get('BACKEND_URL', 'http://localhost:8000')
FIRMS_API_KEY = os.environ.get('FIRMS_API_KEY', '')
SANDBOX_URL = os.environ.get('SANDBOX_URL', 'http://sandbox:8002')

# Import shared routing utilities
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from geo_processing.routing import build_route, find_nearest_fire_stations

mcp = FastMCP(
    'fire-monitor',
    instructions="""
    Fire Monitor MCP server. Provides tools for:
    - Searching active fire hotspots from VIIRS/MODIS satellites
    - Finding Sentinel-2 scenes for burn severity analysis
    - Getting fire statistics by region
    - Building driving routes between points (OSRM)
    - Finding nearest fire stations (OpenStreetMap)
    - Executing custom Python code for geospatial analysis (sandbox)

    All coordinates use WGS84 (EPSG:4326).
    Bounding boxes are [minx, miny, maxx, maxy].
    """,
)


@mcp.tool()
async def search_fires(
    bbox: list[float] | None = None,
    source: str = 'all',
    days: int = 1,
    min_confidence: str = 'nominal',
) -> dict:
    """
    Search for active fire hotspots from satellite data.

    Args:
        bbox: Bounding box [minx, miny, maxx, maxy] in WGS84. If None, returns global data.
        source: Satellite source - 'VIIRS_SNPP', 'VIIRS_NOAA20', 'MODIS_Aqua', 'MODIS_Terra', or 'all'
        days: Number of days to look back (1 or 10)
        min_confidence: Minimum confidence level - 'low', 'nominal', or 'high'

    Returns:
        GeoJSON FeatureCollection with fire hotspot points, including brightness,
        confidence, FRP, and detection time.
    """
    params = {
        'source': source,
        'days': days,
        'min_confidence': min_confidence,
    }
    if bbox and len(bbox) == 4:
        params['bbox'] = ','.join(str(x) for x in bbox)

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(f'{BACKEND_URL}/api/fires/', params=params)
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def search_sentinel2(
    bbox: list[float],
    start_date: str,
    end_date: str,
    max_cloud_cover: int = 30,
    limit: int = 10,
) -> dict:
    """
    Search for Sentinel-2 satellite scenes for burn severity analysis.

    Args:
        bbox: Bounding box [minx, miny, maxx, maxy] in WGS84
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        max_cloud_cover: Maximum cloud cover percentage (0-100)
        limit: Maximum number of results

    Returns:
        List of available Sentinel-2 scenes with dates and cloud cover.
    """
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f'{BACKEND_URL}/api/jobs/',
            json={
                'job_type': 'sentinel2_search',
                'params': {
                    'bbox': bbox,
                    'start_date': start_date,
                    'end_date': end_date,
                    'max_cloud_cover': max_cloud_cover,
                    'limit': limit,
                },
            },
        )
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def get_fire_stats(
    bbox: list[float] | None = None,
    days: int = 7,
) -> dict:
    """
    Get aggregated fire statistics for a region.

    Args:
        bbox: Bounding box [minx, miny, maxx, maxy] in WGS84. If None, global stats.
        days: Number of days to look back

    Returns:
        Statistics including total hotspot count, breakdown by source and confidence,
        average brightness and FRP.
    """
    params = {'days': days}
    if bbox and len(bbox) == 4:
        params['bbox'] = ','.join(str(x) for x in bbox)

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(f'{BACKEND_URL}/api/fires/stats/', params=params)
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def get_burn_stats(
    bbox: list[float] | None = None,
) -> dict:
    """
    Get burn area statistics.

    Args:
        bbox: Bounding box [minx, miny, maxx, maxy] in WGS84. If None, global stats.

    Returns:
        Statistics including total burned area, breakdown by severity class.
    """
    params = {}
    if bbox and len(bbox) == 4:
        params['bbox'] = ','.join(str(x) for x in bbox)

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.get(f'{BACKEND_URL}/api/burns/stats/', params=params)
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def start_fire_detection(
    bbox: list[float],
    sources: list[str] | None = None,
) -> dict:
    """
    Start a fire detection job for a specific area.
    Downloads latest VIIRS/MODIS data, applies filtering, and stores results.

    Args:
        bbox: Bounding box [minx, miny, maxx, maxy] in WGS84
        sources: List of satellite sources to use (default: all VIIRS + MODIS)

    Returns:
        Job ID and status for tracking progress.
    """
    if sources is None:
        sources = ['VIIRS_SNPP', 'VIIRS_NOAA20', 'MODIS_Aqua', 'MODIS_Terra']

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f'{BACKEND_URL}/api/jobs/',
            json={
                'job_type': 'fire_detection',
                'bbox': {
                    'type': 'Polygon',
                    'coordinates': [[
                        [bbox[0], bbox[1]],
                        [bbox[2], bbox[1]],
                        [bbox[2], bbox[3]],
                        [bbox[0], bbox[3]],
                        [bbox[0], bbox[1]],
                    ]],
                },
                'params': {'sources': sources},
            },
        )
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def start_burn_mapping(
    bbox: list[float],
    pre_date: str,
    post_date: str,
) -> dict:
    """
    Start a burn severity mapping job using Sentinel-2 dNBR analysis.

    Requires pre-fire and post-fire dates. The system will find the closest
    available Sentinel-2 scenes and calculate burn severity.

    Args:
        bbox: Bounding box [minx, miny, maxx, maxy] in WGS84
        pre_date: Pre-fire date (YYYY-MM-DD) — before the fire
        post_date: Post-fire date (YYYY-MM-DD) — after the fire

    Returns:
        Job ID and status for tracking progress.
    """
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f'{BACKEND_URL}/api/jobs/',
            json={
                'job_type': 'burn_mapping',
                'bbox': {
                    'type': 'Polygon',
                    'coordinates': [[
                        [bbox[0], bbox[1]],
                        [bbox[2], bbox[1]],
                        [bbox[2], bbox[3]],
                        [bbox[0], bbox[3]],
                        [bbox[0], bbox[1]],
                    ]],
                },
                'params': {
                    'pre_date': pre_date,
                    'post_date': post_date,
                },
            },
        )
        response.raise_for_status()
        return response.json()


# =============================================================================
# ROUTING AND GEOLOCATION TOOLS
# =============================================================================

@mcp.tool()
async def build_route_mcp(
    start_lon: float,
    start_lat: float,
    end_lon: float,
    end_lat: float,
) -> dict:
    """
    Build a driving route between two points using OSRM (Open Source Routing Machine).
    
    Returns GeoJSON with route geometry, distance (km), and duration (minutes).
    The route follows actual roads, not straight lines.
    
    Args:
        start_lon: Start point longitude
        start_lat: Start point latitude
        end_lon: End point longitude
        end_lat: End point latitude
    
    Returns:
        Dict with geojson (FeatureCollection containing route line + start/end points),
        distance_km, duration_min, and success flag.
    """
    start = [start_lon, start_lat]
    end = [end_lon, end_lat]
    return build_route(start, end)


@mcp.tool()
async def find_nearest_fire_stations_mcp(
    lat: float | None = None,
    lon: float | None = None,
    radius_km: int = 50,
    limit: int | None = None,
    bbox: list[float] | None = None,
) -> dict:
    """
    Find fire stations using OpenStreetMap (Overpass API).
    
    Can search in two modes:
    1. Around a point (lat, lon, radius_km) - returns nearest stations
    2. In a bounding box (bbox) - returns all stations in area
    
    By default (no limit), returns ALL found stations.

    Args:
        lat: Latitude of search center (optional if bbox provided)
        lon: Longitude of search center (optional if bbox provided)
        radius_km: Search radius in kilometers (default: 50, used only with lat/lon)
        limit: Maximum number of stations to return (default: None = return all found)
        bbox: Bounding box [min_lon, min_lat, max_lon, max_lat] to search in (alternative to lat/lon)

    Returns:
        Dict with geojson (FeatureCollection of fire stations),
        stations list, total_found count, returned_count, and search_mode.
    """
    return find_nearest_fire_stations(lat, lon, radius_km, limit, bbox)


@mcp.tool()
async def execute_python_mcp(
    code: str,
    context: dict | None = None,
) -> dict:
    """
    Execute custom Python code in a secure sandbox for geospatial analysis.
    
    The sandbox has pre-imported libraries:
    - numpy (as np)
    - pandas (as pd)
    - geopandas (as gpd)
    - shapely.geometry: Point, LineString, Polygon, shape, mapping, box
    - shapely.ops: unary_union, transform
    
    IMPORTANT: Do NOT write 'import' statements — all libraries are pre-imported.
    NO network access is allowed inside the sandbox.
    
    To return GeoJSON for map display, assign it to the __result__ variable.
    
    Args:
        code: Python code to execute (without import statements)
        context: Optional dict of variables to inject (e.g., GeoJSON from previous tools)
    
    Returns:
        Dict with success flag, stdout, stderr, and result (GeoJSON if __result__ was set).
    
    Example:
        code: "gdf = gpd.GeoDataFrame.from_features(context['fires']['features']); __result__ = gdf.__geo_interface__"
        context: {"fires": geojson_from_search_fires}
    """
    if context is None:
        context = {}
    
    async with httpx.AsyncClient(timeout=35.0) as client:
        response = await client.post(
            f'{SANDBOX_URL}/execute',
            json={'code': code, 'context': context}
        )
    
    if response.status_code == 400:
        return {"error": f"Security violation: {response.json().get('detail')}", "success": False}
    if response.status_code == 408:
        return {"error": "Timeout: Code execution exceeded 30s limit", "success": False}
    if response.status_code == 429:
        return {"error": "Rate limit exceeded", "success": False}
    if response.status_code != 200:
        return {"error": f"Sandbox error: {response.text}", "success": False}
    
    return response.json()


@mcp.resource('fire-monitor://info')
async def service_info() -> str:
    """Get information about the Fire Monitor service."""
    return json.dumps({
        'service': 'Fire Monitor',
        'version': '0.2.0',
        'description': 'AI-powered wildfire monitoring using satellite data',
        'data_sources': ['VIIRS_SNPP', 'VIIRS_NOAA20', 'MODIS_Aqua', 'MODIS_Terra', 'Sentinel-2'],
        'tools': [
            'search_fires',
            'search_sentinel2',
            'get_fire_stats',
            'get_burn_stats',
            'start_fire_detection',
            'start_burn_mapping',
            'build_route_mcp',
            'find_nearest_fire_stations_mcp',
            'execute_python_mcp',
        ],
    }, indent=2)


if __name__ == '__main__':
    mcp.run(transport='streamable-http', host='0.0.0.0', port=8001)
