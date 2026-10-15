"""
Multi-tile burn severity mapping.

Covers the whole requested region instead of a single Sentinel-2 granule:
finds every Sentinel-2 tile intersecting the bbox, selects the clearest
pre/post scene pair per tile and runs the dNBR analysis tile by tile.

Shared by the chat handler, the REST API and the management command, so
scene selection, tile orchestration and vectorization live in one place.
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np
from django.conf import settings
from django.contrib.gis.geos import GEOSGeometry
from django.db import connection as django_db_connection
from pyproj import Transformer
from rasterio.features import shapes
from shapely.geometry import MultiPolygon, shape
from shapely.ops import unary_union, transform as shapely_transform

from fires.models import BurnArea
from geo_processing.burn_severity import process_sentinel2_burn
from geo_processing.stac_client import (
    cloud_cover_of,
    get_scl_asset,
    get_sentinel2_assets,
    search_sentinel2,
    tile_id_of,
)

logger = logging.getLogger(__name__)

# GDAL HTTP options for remote raster reads. Without explicit timeouts a
# stalled Azure connection hangs the worker thread forever (observed
# 2026-10-14: a tile stuck 17+ minutes with no data and no error). With a
# timeout the read fails and the per-tile retry loop handles it.
os.environ.setdefault('GDAL_HTTP_CONNECTTIMEOUT', '30')
os.environ.setdefault('GDAL_HTTP_TIMEOUT', '300')

SEVERITY_CLASSES = ['low', 'moderate', 'high']

# Fetch every scene in the date window, then group by tile locally
STAC_SEARCH_LIMIT = 500

# Vectorization parameters (same as the previous single-tile implementation)
MIN_AREA_HA = 1.0
SIMPLIFY_TOLERANCE = 0.005
VECTOR_DOWNSAMPLE = 4

# Transient errors from Planetary Computer / Azure worth retrying: HTTP
# 403/408/429/5xx (throttling bursts) and network-level failures (timeouts,
# resets, connection errors from GDAL's curl-based HTTP driver).
_TRANSIENT_HTTP_RE = re.compile(
    r'HTTP response code: (403|408|429|5\d\d)'
    r'|timed? ?out'
    r'|connection (was )?(reset|refused|broken|aborted)'
    r'|failed to connect'
    r'|curl error'
    r'|network is unreachable',
    re.IGNORECASE,
)


def _is_transient_http_error(exc):
    return bool(_TRANSIENT_HTTP_RE.search(str(exc)))


def _process_tile(tile, pre_scene, post_scene, bbox, pre_date, post_date):
    """
    Download and analyze a single tile, retrying on transient HTTP errors
    (Planetary Computer throttling returns short 403 bursts).

    Asset URLs are re-signed on every attempt. The per-thread Django DB
    connection is closed at the end, since worker threads do not close it
    automatically.
    """
    delays = settings.BURN_RETRY_DELAYS
    try:
        for attempt in range(len(delays) + 1):
            try:
                pre_assets = get_sentinel2_assets(pre_scene, bands=['B08', 'B12'])
                post_assets = get_sentinel2_assets(post_scene, bands=['B08', 'B12'])
                result = process_sentinel2_burn(
                    pre_assets=pre_assets,
                    post_assets=post_assets,
                    bbox=bbox,
                    n_classes=4,
                    pre_scl_url=get_scl_asset(pre_scene),
                    post_scl_url=get_scl_asset(post_scene),
                    apply_cloud_masking=True,
                )
                tile_features = _vectorize_and_save(
                    severity_data=result['severity'],
                    stats=result['stats'],
                    transform=result['transform'],
                    source_crs=result['crs'],
                    pre_date=pre_date,
                    post_date=post_date,
                    tile_id=pre_scene.id,
                )
                return {'result': result, 'features': tile_features}
            except Exception as e:
                if attempt < len(delays) and _is_transient_http_error(e):
                    delay = delays[attempt]
                    logger.warning(
                        f'Tile {tile}: attempt {attempt + 1}/{len(delays) + 1} failed ({e}); '
                        f'retrying in {delay}s'
                    )
                    time.sleep(delay)
                    continue
                raise
    finally:
        django_db_connection.close()


def select_best_scenes_by_tile(items):
    """Group scenes by MGRS tile, keeping the clearest scene per tile."""
    best = {}
    for item in items:
        tile = tile_id_of(item)
        if not tile:
            continue
        current = best.get(tile)
        if current is None or cloud_cover_of(item) < cloud_cover_of(current):
            best[tile] = item
    return best


def _vectorize_and_save(severity_data, stats, transform, source_crs, pre_date, post_date, tile_id):
    """
    Vectorize one tile's severity masks, save BurnArea records and return
    the created GeoJSON features.

    Raster geometries from shapes() are in the tile's UTM units (meters),
    so polygon area is in m2 (hectares = m2 / 10000).
    """
    transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
    project = lambda x, y: transformer.transform(x, y)

    features = []
    for severity_class in SEVERITY_CLASSES:
        if severity_class not in stats or stats[severity_class]['area_ha'] == 0:
            continue

        mask = (severity_data == severity_class).astype(np.uint8)
        if VECTOR_DOWNSAMPLE > 1:
            mask_downsampled = mask[::VECTOR_DOWNSAMPLE, ::VECTOR_DOWNSAMPLE]
            downsampled_transform = transform * transform.scale(VECTOR_DOWNSAMPLE, VECTOR_DOWNSAMPLE)
        else:
            mask_downsampled = mask
            downsampled_transform = transform

        polygons = []
        for geom, value in shapes(mask_downsampled.astype(np.uint8), transform=downsampled_transform):
            if value == 1:
                try:
                    poly = shape(geom)
                    if poly.is_valid and poly.area > 0 and poly.area / 10000 >= MIN_AREA_HA:
                        polygons.append(poly)
                except Exception:
                    continue

        if not polygons:
            continue

        merged = unary_union(polygons)
        merged = merged.simplify(SIMPLIFY_TOLERANCE, preserve_topology=True)
        merged_wgs84 = shapely_transform(project, merged)

        # BurnArea.geometry is a MultiPolygonField — wrap single polygons
        if merged_wgs84.geom_type == 'Polygon':
            merged_wgs84 = MultiPolygon([merged_wgs84])
        elif merged_wgs84.geom_type == 'MultiPolygon':
            merged_wgs84 = MultiPolygon(merged_wgs84.geoms)
        else:
            continue

        try:
            geos_geom = GEOSGeometry(merged_wgs84.wkt, srid=4326)
            burn_area = BurnArea.objects.create(
                geometry=geos_geom,
                area_ha=stats[severity_class]['area_ha'],
                severity=severity_class,
                dnbr_mean=stats[severity_class]['mean_dnbr'],
                pre_date=pre_date,
                post_date=post_date,
                satellite='Sentinel-2',
                tile_id=tile_id,
            )
        except Exception as e:
            logger.error(f'Failed to save burn area ({severity_class}, tile {tile_id}): {e}')
            continue

        features.append({
            'type': 'Feature',
            'geometry': json.loads(burn_area.geometry.geojson),
            'properties': {
                'severity': severity_class,
                'area_ha': stats[severity_class]['area_ha'],
                'mean_dnbr': stats[severity_class]['mean_dnbr'],
                'pre_date': str(pre_date),
                'post_date': str(post_date),
                'tile_id': tile_id,
            },
        })

    return features


def _map_burns_multitile_core(bbox, pre_date, post_date, max_cloud_cover=30, max_tiles=None, window_days=None, pre_items=None, post_items=None, progress_cb=None):
    """
    Core multi-tile burn severity mapping (no ProcessingJob bookkeeping —
    the public entry point map_burns_multitile() wraps this with job
    tracking and the initial STAC search).

    pre_items/post_items: pre-computed STAC item lists (from the caller's
    STAC search); searched here if not provided.
    progress_cb(done, total, tile_results, tile_errors): called after each
    tile finishes, for live progress reporting.

    Args:
        bbox: (minx, miny, maxx, maxy) in WGS84
        pre_date: pre-fire reference date, YYYY-MM-DD (fires start after it)
        post_date: post-fire reference date, YYYY-MM-DD (fires ended by it)
        max_cloud_cover: maximum accepted scene cloud cover (%)
        max_tiles: max number of tiles to process (default settings.BURN_MAX_TILES)
        window_days: scene search window before pre_date / after post_date
                     (default settings.BURN_SCENE_WINDOW_DAYS)

    Returns:
        Dict with geojson (FeatureCollection), data_type ('burn') and summary:
        tile coverage (tiles_total/tiles_processed/tiles_failed/tiles_skipped),
        per-tile details, total burned area and severity breakdown.
        On failure (e.g. no cloud-free scenes) summary contains 'error'.
    """
    if max_tiles is None:
        max_tiles = settings.BURN_MAX_TILES
    if window_days is None:
        window_days = settings.BURN_SCENE_WINDOW_DAYS

    pre_date = str(pre_date)[:10]
    post_date = str(post_date)[:10]
    pre_obj = datetime.strptime(pre_date, '%Y-%m-%d').date()
    post_obj = datetime.strptime(post_date, '%Y-%m-%d').date()
    if pre_obj >= post_obj:
        raise ValueError(f'pre_date ({pre_date}) must be before post_date ({post_date})')

    pre_start = (pre_obj - timedelta(days=window_days)).isoformat()
    post_end = (post_obj + timedelta(days=window_days)).isoformat()
    bbox = tuple(float(x) for x in bbox)

    if not pre_items or not post_items:
        return {
            'geojson': {'type': 'FeatureCollection', 'features': []},
            'data_type': 'burn',
            'summary': {
                'error': (
                    f'No Sentinel-2 scenes with cloud cover < {max_cloud_cover}% for the '
                    f'requested windows: pre [{pre_start}..{pre_date}] ({len(pre_items)} found), '
                    f'post [{post_date}..{post_end}] ({len(post_items)} found). '
                    f'Try a wider date range or higher max_cloud_cover.'
                ),
                'tiles_total': 0,
                'tiles_processed': 0,
                'tiles_failed': 0,
                'tiles_skipped': [],
                'tile_details': [],
                'total_burned_ha': 0,
                'by_severity': {},
                'polygons_loaded': 0,
            },
        }

    pre_by_tile = select_best_scenes_by_tile(pre_items)
    post_by_tile = select_best_scenes_by_tile(post_items)

    tiles = sorted(set(pre_by_tile) & set(post_by_tile))
    if not tiles:
        return {
            'geojson': {'type': 'FeatureCollection', 'features': []},
            'data_type': 'burn',
            'summary': {
                'error': 'No Sentinel-2 tile has both a pre and a post scene for the requested windows.',
                'tiles_total': 0,
                'tiles_processed': 0,
                'tiles_failed': 0,
                'tiles_skipped': [],
                'tile_details': [],
                'total_burned_ha': 0,
                'by_severity': {},
                'polygons_loaded': 0,
            },
        }

    # Clearest pairs first, so the tile cap keeps the best data
    tiles.sort(key=lambda t: max(cloud_cover_of(pre_by_tile[t]), cloud_cover_of(post_by_tile[t])))
    selected_tiles = tiles[:max_tiles]
    skipped_tiles = tiles[max_tiles:]

    # Process tiles in parallel (each tile is independent: download + dNBR + save).
    # Keep the worker count modest so we do not trip Planetary Computer throttling.
    workers = max(1, min(settings.BURN_PARALLEL_TILES, len(selected_tiles)))
    logger.info(f'Processing {len(selected_tiles)} tiles with {workers} parallel workers')

    tile_results = {}
    tile_errors = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_process_tile, tile, pre_by_tile[tile], post_by_tile[tile], bbox, pre_date, post_date): tile
            for tile in selected_tiles
        }
        for future in as_completed(futures):
            tile = futures[future]
            done += 1
            try:
                tile_results[tile] = future.result()
            except Exception as e:
                tile_errors[tile] = str(e)
                logger.exception(f'Tile {tile} failed: {e}')
            logger.info(f'Progress: {done}/{len(selected_tiles)} tiles finished')
            if progress_cb:
                progress_cb(done, len(selected_tiles), tile_results, tile_errors)

    features = []
    tile_details = []
    total_burned = 0.0
    by_severity_agg = {}
    processed = 0
    failed = 0

    for tile in selected_tiles:  # deterministic order in the summary
        pre_scene = pre_by_tile[tile]
        post_scene = post_by_tile[tile]
        detail = {
            'tile': tile,
            'pre_scene': pre_scene.id,
            'post_scene': post_scene.id,
            'pre_cloud_cover': round(cloud_cover_of(pre_scene), 1),
            'post_cloud_cover': round(cloud_cover_of(post_scene), 1),
        }
        if tile in tile_errors:
            failed += 1
            detail['error'] = tile_errors[tile]
            tile_details.append(detail)
            continue

        result = tile_results[tile]['result']
        tile_features = tile_results[tile]['features']
        stats = result['stats']
        tile_burned = stats['total_burned_ha']
        total_burned += tile_burned
        for cls in SEVERITY_CLASSES:
            if cls in stats and stats[cls]['area_ha']:
                agg = by_severity_agg.setdefault(cls, {'area_ha': 0.0, 'mean_dnbr_weighted': 0.0})
                agg['area_ha'] += stats[cls]['area_ha']
                agg['mean_dnbr_weighted'] += stats[cls]['mean_dnbr'] * stats[cls]['area_ha']

        detail['burned_ha'] = tile_burned
        detail['polygons'] = len(tile_features)
        features.extend(tile_features)
        processed += 1
        logger.info(f'Tile {tile}: burned {tile_burned} ha, {len(tile_features)} polygons')
        tile_details.append(detail)

    by_severity = {
        cls: {
            'area_ha': round(v['area_ha'], 2),
            'mean_dnbr': round(v['mean_dnbr_weighted'] / v['area_ha'], 1) if v['area_ha'] else 0,
        }
        for cls, v in by_severity_agg.items()
    }

    return {
        'geojson': {'type': 'FeatureCollection', 'features': features},
        'data_type': 'burn',
        'summary': {
            'tiles_total': len(tiles),
            'tiles_processed': processed,
            'tiles_failed': failed,
            'tiles_skipped': skipped_tiles,
            'tile_details': tile_details,
            'total_burned_ha': round(total_burned, 2),
            'by_severity': by_severity,
            'pre_date': pre_date,
            'post_date': post_date,
            'polygons_loaded': len(features),
        },
    }


# ---------------------------------------------------------------------------
# Public entry point with ProcessingJob tracking
# ---------------------------------------------------------------------------

def _create_burn_job(bbox, pre_date, post_date, max_cloud_cover):
    from django.contrib.gis.geos import Polygon as DjangoPolygon
    from fires.models import ProcessingJob

    return ProcessingJob.objects.create(
        job_type='burn_mapping',
        status='running',
        bbox=DjangoPolygon.from_bbox(tuple(float(x) for x in bbox)),
        params={
            'pre_date': pre_date,
            'post_date': post_date,
            'max_cloud_cover': max_cloud_cover,
        },
        started_at=datetime.now(),
    )


def _finalize_burn_job(job, status, summary):
    try:
        job.status = status
        job.completed_at = datetime.now()
        job.result_summary = summary
        if status == 'failed':
            job.error_message = summary.get('error', 'Unknown error')
        job.save()
    except Exception:
        logger.exception(f'Failed to update burn job {getattr(job, "id", "?")}')


def _report_burn_progress(job, done, total, tile_results, tile_errors):
    burned_so_far = sum(out['result']['stats']['total_burned_ha'] for out in tile_results.values())
    polygons_so_far = sum(len(out['features']) for out in tile_results.values())
    job.result_summary = {
        'status': 'running',
        'tiles_done': done,
        'tiles_total': total,
        'tiles_processed': len(tile_results),
        'tiles_failed': len(tile_errors),
        'total_burned_ha_so_far': round(burned_so_far, 2),
        'polygons_so_far': polygons_so_far,
    }
    job.save()


def map_burns_multitile(bbox, pre_date, post_date, max_cloud_cover=30, max_tiles=None, window_days=None, job=None):
    """
    Map burn severity across all Sentinel-2 tiles covering bbox.

    Tracks the run in a ProcessingJob record (status + live tile progress in
    result_summary) so clients can poll progress and the map can render
    polygons as tiles complete. Pass job= to use an existing ProcessingJob
    instead of creating a new one.

    Args:
        bbox: (minx, miny, maxx, maxy) in WGS84
        pre_date: pre-fire reference date, YYYY-MM-DD (fires start after it)
        post_date: post-fire reference date, YYYY-MM-DD (fires ended by it)
        max_cloud_cover: maximum accepted scene cloud cover (%)
        max_tiles: max number of tiles to process (default settings.BURN_MAX_TILES)
        window_days: scene search window before pre_date / after post_date
                     (default settings.BURN_SCENE_WINDOW_DAYS)
        job: optional existing ProcessingJob to track the run in

    Returns:
        Dict with geojson (FeatureCollection), data_type ('burn') and summary:
        tile coverage (tiles_total/tiles_processed/tiles_failed/tiles_skipped),
        per-tile details, total burned area and severity breakdown.
        On failure (e.g. no cloud-free scenes) summary contains 'error'.
    """
    pre_date = str(pre_date)[:10]
    post_date = str(post_date)[:10]
    pre_obj = datetime.strptime(pre_date, '%Y-%m-%d').date()
    post_obj = datetime.strptime(post_date, '%Y-%m-%d').date()
    if pre_obj >= post_obj:
        raise ValueError(f'pre_date ({pre_date}) must be before post_date ({post_date})')

    if max_tiles is None:
        max_tiles = settings.BURN_MAX_TILES
    if window_days is None:
        window_days = settings.BURN_SCENE_WINDOW_DAYS

    bbox = tuple(float(x) for x in bbox)

    job = job or _create_burn_job(bbox, pre_date, post_date, max_cloud_cover)
    logger.info(
        f'Burn mapping job {job.id} started: bbox={bbox}, '
        f'pre={pre_date}, post={post_date}'
    )

    pre_start = (pre_obj - timedelta(days=window_days)).isoformat()
    post_end = (post_obj + timedelta(days=window_days)).isoformat()
    logger.info(
        f'Multi-tile burn mapping: bbox={bbox}, '
        f'pre window [{pre_start}..{pre_date}], post window [{post_date}..{post_end}]'
    )
    pre_items = search_sentinel2(
        bbox=bbox, start_date=pre_start, end_date=pre_date,
        max_cloud_cover=max_cloud_cover, limit=STAC_SEARCH_LIMIT,
    )
    post_items = search_sentinel2(
        bbox=bbox, start_date=post_date, end_date=post_end,
        max_cloud_cover=max_cloud_cover, limit=STAC_SEARCH_LIMIT,
    )
    logger.info(f'Sentinel-2 scenes found: {len(pre_items)} pre, {len(post_items)} post')

    def _progress(done, total, tile_results, tile_errors):
        try:
            _report_burn_progress(job, done, total, tile_results, tile_errors)
        except Exception:
            logger.exception('Failed to update burn job progress')

    try:
        result = _map_burns_multitile_core(
            bbox=bbox,
            pre_date=pre_date,
            post_date=post_date,
            max_cloud_cover=max_cloud_cover,
            max_tiles=max_tiles,
            window_days=window_days,
            pre_items=pre_items,
            post_items=post_items,
            progress_cb=_progress,
        )
    except Exception as e:
        _finalize_burn_job(job, 'failed', {'error': str(e)})
        raise

    _finalize_burn_job(
        job,
        'failed' if 'error' in result['summary'] else 'completed',
        result['summary'],
    )
    return result