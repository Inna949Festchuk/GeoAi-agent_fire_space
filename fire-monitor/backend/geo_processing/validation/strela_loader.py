"""
"Strela" (Fire Monitor) outputs loader for validation.

Sources:
- PostGIS database (FireHotspot / BurnArea) — the system's own data;
- GeoJSON files in data/strela/ (viirs_detections.geojson, burned_areas.geojson) — optional.

source semantics:
  'auto'  — files if they exist and yield data, otherwise the database;
  'files' — GeoJSON files only (error when missing/empty);
  'db'    — database only.
"""
import json
import logging
from datetime import timedelta
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import shape

from .config import STRELA_DIR, ValidationDataError
from .config import BURN_SEVERITIES, MIN_BURN_AREA_HA

logger = logging.getLogger(__name__)

VIIRS_FILE = 'viirs_detections.geojson'
BURNS_FILE = 'burned_areas.geojson'

_DATE_CANDIDATES = ['detected_at', 'detection_time', 'datetime', 'acq_time', 'date', 'time']
_BURN_DATE_CANDIDATES = ['burn_date', 'post_date', 'mapping_date', 'date']


def _parse_bbox(bbox):
    return tuple(float(x) for x in bbox)


def _load_geojson(path):
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs('EPSG:4326')
    else:
        gdf = gdf.to_crs('EPSG:4326')
    return gdf


def _extract_date(gdf, candidates):
    """Find the first candidate column that parses as datetimes.

    Returns (gdf_with_column, column_name or None).
    """
    for col in candidates:
        if col not in gdf.columns:
            continue
        try:
            s = pd.to_datetime(gdf[col], errors='coerce')
        except (TypeError, ValueError):
            s = pd.to_datetime(gdf[col], errors='coerce', format='mixed')
        if s.notna().any():
            gdf = gdf.copy()
            gdf[col] = s
            return gdf, col
    return gdf, None


def _filter_bbox(gdf, bbox):
    x0, y0, x1, y1 = _parse_bbox(bbox)

    def intersects(geom):
        try:
            b = geom.bounds
            return b[0] <= x1 and b[2] >= x0 and b[1] <= y1 and b[3] >= y0
        except Exception:
            return False

    return gdf[gdf.geometry.map(intersects)].copy()


# ---------------------------------------------------------------------------
# Fire detections (points)
# ---------------------------------------------------------------------------

def _load_fires_file(path, bbox, start, end):
    gdf = _load_geojson(path)
    gdf, date_col = _extract_date(gdf, _DATE_CANDIDATES)
    if date_col:
        gdf = gdf[(gdf[date_col] >= start) & (gdf[date_col] < end)]
        gdf = gdf.rename(columns={date_col: 'detected_at'})
    gdf = _filter_bbox(gdf, bbox)
    return gdf.reset_index(drop=True)


def _load_fires_from_db(bbox, start, end):
    from django.contrib.gis.geos import Polygon
    from fires.models import FireHotspot

    poly = Polygon.from_bbox(_parse_bbox(bbox))
    objs = list(FireHotspot.objects.only(
        'detected_at', 'frp', 'confidence', 'source', 'location'
    ).filter(
        location__within=poly,
        detected_at__gte=start,
        detected_at__lt=end,
    ).order_by('detected_at'))
    if not objs:
        raise ValidationDataError(
            f'No Strela fire detections in the database for bbox={bbox}, '
            f'{start:%Y-%m-%d}..{end - timedelta(days=1):%Y-%m-%d}. '
            'Run fire detection for this area/period first (fetch-fires) '
            f'or put GeoJSON in {STRELA_DIR / VIIRS_FILE}.'
        )
    gdf = gpd.GeoDataFrame(
        {
            'detected_at': [o.detected_at for o in objs],
            'frp': [o.frp for o in objs],
            'confidence': [o.confidence for o in objs],
            'source': [o.source for o in objs],
        },
        geometry=[shape(json.loads(o.location.json)) for o in objs],
        crs='EPSG:4326',
    )
    return gdf.reset_index(drop=True)


def load_strela_fires(bbox, start_date, end_date, source='auto'):
    """
    Load Strela active fire detections for bbox and period (end inclusive).

    Returns GeoDataFrame (WGS84 points) with detected_at, frp, confidence, source.
    Raises ValidationDataError with an actionable message when nothing is found.
    """
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date) + timedelta(days=1)
    path = STRELA_DIR / VIIRS_FILE

    if source == 'files':
        if not path.exists():
            raise ValidationDataError(f'Strela fire detections file is missing: {path}')
        gdf = _load_fires_file(path, bbox, start, end)
        if gdf.empty:
            raise ValidationDataError(
                f'No Strela fire detections in {path} for bbox={bbox}, '
                f'{start_date}..{end_date}.')
        return gdf

    if source == 'auto' and path.exists():
        gdf = _load_fires_file(path, bbox, start, end)
        if not gdf.empty:
            return gdf

    if source in ('db', 'auto'):
        return _load_fires_from_db(bbox, start, end)

    raise ValidationDataError(f'Unknown source: {source!r} (use auto|db|files)')


# ---------------------------------------------------------------------------
# Burn polygons
# ---------------------------------------------------------------------------

def _load_burns_file(path, bbox, start, end):
    gdf = _load_geojson(path)
    gdf, date_col = _extract_date(gdf, _BURN_DATE_CANDIDATES)
    if date_col:
        gdf = gdf.rename(columns={date_col: 'burn_date'})
        if 'pre_date' in gdf.columns:
            # A mapping covers the period when its scene pair brackets it:
            # pre_date <= end and post_date >= start
            pre = pd.to_datetime(gdf['pre_date'], errors='coerce')
            post = pd.to_datetime(gdf['burn_date'], errors='coerce')
            gdf = gdf[(post >= start) & (pre <= end)]
        else:
            gdf = gdf[(gdf['burn_date'] >= start) & (gdf['burn_date'] < end)]
    else:
        gdf = gdf.copy()
        # No date at all: assume the polygons belong to the period (middle date)
        gdf['burn_date'] = start + (end - start) / 2
    if 'severity' in gdf.columns:
        gdf = gdf[gdf['severity'].isin(BURN_SEVERITIES)]
    if 'area_ha' in gdf.columns:
        gdf = gdf[gdf['area_ha'] >= MIN_BURN_AREA_HA]
    gdf = _filter_bbox(gdf, bbox)
    return gdf.reset_index(drop=True)


def _load_burns_from_db(bbox, start, end, allow_empty=False):
    from django.contrib.gis.geos import Polygon
    from fires.models import BurnArea

    poly = Polygon.from_bbox(_parse_bbox(bbox))
    objs = list(BurnArea.objects.filter(
        geometry__intersects=poly,
        # A mapping covers the period when its scene pair brackets it:
        # pre_date <= end and post_date >= start
        pre_date__lte=end.date(),
        post_date__gte=start.date(),
        severity__in=BURN_SEVERITIES,
        area_ha__gte=MIN_BURN_AREA_HA,
    ))
    if not objs:
        if allow_empty:
            return gpd.GeoDataFrame(
                columns=['area_ha', 'severity', 'dnbr_mean', 'tile_id', 'burn_date'],
                geometry=[], crs='EPSG:4326')
        raise ValidationDataError(
            f'No Strela burn polygons in the database covering bbox={bbox}, '
            f'{start:%Y-%m-%d}..{end - timedelta(days=1):%Y-%m-%d} '
            f'(severity {"/".join(BURN_SEVERITIES)}, area >= {MIN_BURN_AREA_HA:.0f} ha). '
            'Run burn mapping first (map_burns) '
            f'or put GeoJSON in {STRELA_DIR / BURNS_FILE}.'
        )
    gdf = gpd.GeoDataFrame(
        {
            'area_ha': [o.area_ha for o in objs],
            'severity': [o.severity for o in objs],
            'dnbr_mean': [o.dnbr_mean for o in objs],
            'tile_id': [o.tile_id for o in objs],
            'burn_date': pd.to_datetime([o.post_date for o in objs]),
        },
        geometry=[shape(json.loads(o.geometry.json)) for o in objs],
        crs='EPSG:4326',
    )
    return gdf.reset_index(drop=True)


def load_strela_burns(bbox, start_date, end_date, source='auto'):
    """
    Load Strela burn scar polygons for bbox and period (end inclusive).

    Returns GeoDataFrame (WGS84 polygons) with burn_date, area_ha, severity.
    Raises ValidationDataError with an actionable message when nothing is found.
    """
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date) + timedelta(days=1)
    path = STRELA_DIR / BURNS_FILE

    if source == 'files':
        if not path.exists():
            raise ValidationDataError(f'Strela burn polygons file is missing: {path}')
        gdf = _load_burns_file(path, bbox, start, end)
        if gdf.empty:
            raise ValidationDataError(
                f'No Strela burn polygons in {path} for bbox={bbox}, '
                f'{start_date}..{end_date}.')
        return gdf

    if source == 'auto' and path.exists():
        gdf = _load_burns_file(path, bbox, start, end)
        if not gdf.empty:
            return gdf

    if source in ('db', 'auto'):
        return _load_burns_from_db(bbox, start, end)

    raise ValidationDataError(f'Unknown source: {source!r} (use auto|db|files)')