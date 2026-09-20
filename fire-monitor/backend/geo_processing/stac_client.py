"""
Microsoft Planetary Computer STAC client.

Searches and downloads Sentinel-2 and Landsat imagery for burn severity analysis.
"""

import logging
from datetime import datetime, date
from typing import Optional

import pystac_client
import planetary_computer as pc

logger = logging.getLogger(__name__)

# STAC API endpoint
PC_STAC_URL = 'https://planetarycomputer.microsoft.com/api/stac/v1'

# Sentinel-2 collection
SENTINEL2_COLLECTION = 'sentinel-2-l2a'

# Landsat collections
LANDSAT_C2_L2 = 'landsat-c2-l2'


def get_catalog():
    """Get the Planetary Computer STAC catalog."""
    catalog = pystac_client.Client.open(
        PC_STAC_URL,
        modifier=pc.sign_inplace,
    )
    return catalog


def search_sentinel2(
    bbox=None,
    start_date=None,
    end_date=None,
    max_cloud_cover=30,
    limit=10,
):
    """
    Search for Sentinel-2 L2A scenes.

    Args:
        bbox: Bounding box (minx, miny, maxx, maxy) in WGS84
        start_date: Start date (YYYY-MM-DD or date object)
        end_date: End date (YYYY-MM-DD or date object)
        max_cloud_cover: Maximum cloud cover percentage
        limit: Maximum number of results

    Returns:
        List of STAC items with signed assets
    """
    catalog = get_catalog()

    datetime_range = None
    if start_date and end_date:
        if isinstance(start_date, str):
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        if isinstance(end_date, str):
            end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        datetime_range = f'{start_date.isoformat()}/{end_date.isoformat()}'

    search_params = {
        'collections': [SENTINEL2_COLLECTION],
        'limit': limit,
    }

    if bbox:
        search_params['bbox'] = list(bbox)
    if datetime_range:
        search_params['datetime'] = datetime_range
    if max_cloud_cover is not None:
        search_params['query'] = {
            'eo:cloud_cover': {'lt': max_cloud_cover}
        }

    search = catalog.search(**search_params)
    items = list(search.items())

    logger.info(f'Found {len(items)} Sentinel-2 scenes')
    return items


def search_landsat(
    bbox=None,
    start_date=None,
    end_date=None,
    max_cloud_cover=30,
    limit=10,
):
    """Search for Landsat Collection 2 Level-2 scenes."""
    catalog = get_catalog()

    datetime_range = None
    if start_date and end_date:
        if isinstance(start_date, str):
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        if isinstance(end_date, str):
            end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        datetime_range = f'{start_date.isoformat()}/{end_date.isoformat()}'

    search_params = {
        'collections': [LANDSAT_C2_L2],
        'limit': limit,
    }

    if bbox:
        search_params['bbox'] = list(bbox)
    if datetime_range:
        search_params['datetime'] = datetime_range
    if max_cloud_cover is not None:
        search_params['query'] = {
            'eo:cloud_cover': {'lt': max_cloud_cover}
        }

    search = catalog.search(**search_params)
    items = list(search.items())

    logger.info(f'Found {len(items)} Landsat scenes')
    return items


def get_sentinel2_assets(item, bands=None):
    """
    Get signed asset URLs for a Sentinel-2 item.

    Args:
        item: STAC item
        bands: List of band names (default: B04, B08, B11, B12 for burn analysis)

    Returns:
        Dict of band_name -> signed URL
    """
    if bands is None:
        bands = ['B04', 'B08', 'B11', 'B12']

    signed_item = pc.sign(item)
    assets = {}

    for band in bands:
        if band in signed_item.assets:
            assets[band] = signed_item.assets[band].href

    return assets


def get_scl_asset(item):
    """
    Get Scene Classification Layer (SCL) for cloud masking.

    SCL values:
    - 0: No data
    - 1: Saturated or defective
    - 2: Dark area pixels
    - 3: Cloud shadows
    - 4: Vegetation
    - 5: Not vegetated
    - 6: Water
    - 7: Unclassified
    - 8: Cloud medium probability
    - 9: Cloud high probability
    - 10: Thin cirrus
    - 11: Snow

    Args:
        item: STAC item

    Returns:
        Signed URL to SCL band or None if not available
    """
    signed_item = pc.sign(item)

    if 'SCL' in signed_item.assets:
        return signed_item.assets['SCL'].href

    logger.warning('SCL band not available for cloud masking')
    return None


def list_collections(keyword=None):
    """List available STAC collections, optionally filtered by keyword."""
    catalog = get_catalog()
    collections = []

    for collection in catalog.get_collections():
        if keyword is None or keyword.lower() in collection.id.lower():
            collections.append({
                'id': collection.id,
                'title': collection.title or collection.id,
                'description': (collection.description or '')[:200],
            })

    return collections
