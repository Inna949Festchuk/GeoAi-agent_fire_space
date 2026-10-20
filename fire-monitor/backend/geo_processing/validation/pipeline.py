"""
Validation pipeline for the "Strela" system.

run_validation() flow:
  1. Load Rosleshoz 1T-ISM registry (ground truth, points) for bbox + period.
  2. Load Strela outputs (DB or files): fire detections + burn polygons.
  3. Compute metrics:
     - detection: Precision/Recall/F1 (geodesic buffer 500 m, +/-3 days);
     - burns: IoU + area correlation (MCD64A1, +/-5 days).
  4. FP analysis against industrial zones (Overture, from DB).
  5. Write artifacts: reports/validation_report.md, validation_map.html,
     metrics_summary.csv.
"""
import logging
from datetime import datetime, timedelta

import pandas as pd

from .config import (
    BURN_SEVERITIES,
    GROUND_TRUTH_DIR,
    MCD64A1_DIR,
    STRELA_DIR,
    VAL_REPORTS_DIR,
    ValidationDataError,
)
from .metrics import validate_burns, validate_detection
from .nasa_client import cached_mcd64a1_rasters, get_modis_tiles, prepare_mcd64a1
from .reporting import write_reports
from .rosleshoz import ensure_extracted_csv, find_registry_zip, load_rosleshoz
from .strela_loader import BURNS_FILE, VIIRS_FILE, load_strela_burns, load_strela_fires

logger = logging.getLogger(__name__)

MAX_BBOX_DEG = 30.0


def run_validation(bbox, start_date, end_date, source='auto',
                   ensure_data=True, region_name=None):
    """
    Run the full validation pipeline.

    Args:
        bbox: [minx, miny, maxx, maxy] in WGS84
        start_date / end_date: 'YYYY-MM-DD'
        source: 'auto' | 'db' | 'files' — where Strela data comes from
        ensure_data: download missing MCD64A1 tiles when needed
        region_name: human-readable label for the report

    Returns a dict with metrics, artifacts and a compact 'summary' (for the
    LLM / REST client). The '_geo' key carries GeoDataFrames for the map
    and MUST be stripped before JSON serialization.
    """
    bbox = tuple(float(x) for x in bbox)
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)

    if bbox[2] - bbox[0] > MAX_BBOX_DEG or bbox[3] - bbox[1] > MAX_BBOX_DEG:
        raise ValidationDataError(
            f'Bbox too large ({bbox[2] - bbox[0]:.1f}° x {bbox[3] - bbox[1]:.1f}°). '
            f'Maximum is {MAX_BBOX_DEG}° x {MAX_BBOX_DEG}°.')
    if end < start:
        raise ValidationDataError('end_date must be >= start_date')

    warnings = []

    # 1. Ground truth: Rosleshoz 1T-ISM
    gt = load_rosleshoz(bbox, start_date, end_date)
    if gt.empty:
        warnings.append('Рослесхоз: в реестре нет пожаров для этого bbox/периода')

    # 2. Strela fire detections (optional for the pipeline)
    strela_fires = None
    try:
        strela_fires = load_strela_fires(bbox, start_date, end_date, source)
    except ValidationDataError as e:
        warnings.append(f'Детекции: {e}')

    # 3. Strela burn polygons (optional for the pipeline)
    strela_burns = None
    try:
        strela_burns = load_strela_burns(bbox, start_date, end_date, source)
    except ValidationDataError as e:
        warnings.append(f'Гари: {e}')

    # 4. Metrics
    detection = None
    if not gt.empty and strela_fires is not None and not strela_fires.empty:
        detection = validate_detection(gt, strela_fires)
        warnings.extend(detection.pop('warnings', []))

    burns = None
    modis_burn = None
    if strela_burns is not None and not strela_burns.empty:
        tifs = _ensure_mcd64a1(bbox, start, end, ensure_data, warnings)
        if tifs:
            burns = validate_burns(strela_burns, tifs, bbox, start, end)
            if burns:
                warnings.extend(burns.pop('warnings', []))
                # reference MCD64A1 burn mask for the map layer
                modis_burn = burns.pop('_modis_burn', None)
        else:
            warnings.append('Гари: нет данных MCD64A1 — метрики гарей не рассчитаны')

    # 5. Industrial zones (for FP analysis and the map)
    zones_gdf = None
    try:
        zones_gdf = _load_industrial_zones(bbox)
    except Exception as e:
        warnings.append(f'Промзоны недоступны: {e}')

    fp_industrial = None
    if (detection is not None
            and detection.get('fp_points') is not None
            and not detection['fp_points'].empty
            and zones_gdf is not None and not zones_gdf.empty):
        fp_industrial = _fp_industrial_analysis(detection['fp_points'], zones_gdf)

    result = {
        'region_name': region_name or '',
        'bbox': list(bbox),
        'start_date': str(start.date()),
        'end_date': str(end.date()),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'source': source,
        'gt_fires': int(len(gt)),
        'strela_fires': int(len(strela_fires)) if strela_fires is not None else 0,
        'strela_burn_polygons': int(len(strela_burns)) if strela_burns is not None else 0,
        'detection': detection,
        'burns': burns,
        'fp_industrial': fp_industrial,
        'warnings': warnings,
        '_geo': {
            'gt': gt,
            'strela_fires': strela_fires,
            'strela_burns': strela_burns,
            'industrial_zones': zones_gdf,
            'modis_burn': modis_burn,
        },
    }
    if detection:
        result['_geo']['fp_points'] = detection.pop('fp_points', None)
        result['_geo']['fn_points'] = detection.pop('fn_points', None)

    if detection is None and burns is None:
        raise ValidationDataError(
            'Валидация не выполнена: нет сопоставимых данных. '
            + ' | '.join(w for w in warnings if w)[:600])

    artifacts = write_reports(result, VAL_REPORTS_DIR)
    result['artifacts'] = {k: str(v) for k, v in artifacts.items()}
    result['summary'] = build_summary(result)
    return result


def _ensure_mcd64a1(bbox, start, end, ensure_data, warnings):
    """MCD64A1 rasters for the period: download when allowed, else cached only."""
    if ensure_data:
        try:
            tifs, dwarn = prepare_mcd64a1(bbox, start, end)
            warnings.extend(dwarn)
            return tifs
        except ValidationDataError as e:
            warnings.append(f'MCD64A1: {e}')
    return cached_mcd64a1_rasters(bbox, start, end)


def _load_industrial_zones(bbox):
    """IndustrialZone polygons (Overture) intersecting the bbox, or empty GDF."""
    import json as _json
    import geopandas as gpd
    from django.contrib.gis.geos import Polygon
    from shapely.geometry import shape
    from fires.models import IndustrialZone

    poly = Polygon.from_bbox(tuple(float(x) for x in bbox))
    zones = list(IndustrialZone.objects.filter(geometry__intersects=poly))
    if not zones:
        return gpd.GeoDataFrame(
            columns=['name', 'zone_type'], geometry=[], crs='EPSG:4326')
    return gpd.GeoDataFrame(
        {'name': [z.name for z in zones],
         'zone_type': [z.zone_type for z in zones]},
        geometry=[shape(_json.loads(z.geometry.json)) for z in zones],
        crs='EPSG:4326')


def _fp_industrial_analysis(fp_points, zones_gdf, buffer_m=None):
    """How many FP detections sit inside the 500 m buffer of industrial zones."""
    from .config import BUFFER_METERS
    from .metrics import geodesic_buffer
    import geopandas as gpd

    if buffer_m is None:
        buffer_m = BUFFER_METERS
    zones_buf = geodesic_buffer(zones_gdf, buffer_m)
    joined = gpd.sjoin(fp_points, zones_buf, how='inner', predicate='intersects')
    n_near = int(len(joined))
    fp_total = int(len(fp_points))
    note = None
    if n_near:
        share = n_near / fp_total
        note = (f'{n_near} из {fp_total} FP-точек находятся в {buffer_m:.0f} м от '
                'промышленных зон (Overture). '
                + ('Рекомендация: расширить буфер промзон с 500 м до 1000 м.'
                   if share >= 0.3 else
                   'Большинство FP вне промзон — проверьте мелкие незарегистрированные '
                   'пожары (<1 га) и другие источники тепла.'))
    return {
        'zones_in_area': int(len(zones_gdf)),
        'fp_total': fp_total,
        'fp_near_zones': n_near,
        'note': note,
    }


# ---------------------------------------------------------------------------
# Data preparation & status
# ---------------------------------------------------------------------------

def prepare_data(bbox, start_date, end_date):
    """
    Prepare ground truth data (idempotent): extract the Rosleshoz CSV and
    download MCD64A1 tiles for all months of the period. The heavy network
    step — safe to run separately before run_validation.
    """
    bbox = tuple(float(x) for x in bbox)
    start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)

    result = {'rosleshoz': None, 'mcd64a1': [], 'tiles': [], 'warnings': []}

    try:
        csv_path = ensure_extracted_csv()
        result['rosleshoz'] = {
            'csv': csv_path.name,
            'size_mb': round(csv_path.stat().st_size / 1e6, 1),
        }
    except ValidationDataError as e:
        result['warnings'].append(f'Rosleshoz: {e}')

    result['tiles'] = get_modis_tiles(bbox)
    tifs, dwarn = prepare_mcd64a1(bbox, start, end)
    result['warnings'].extend(dwarn)
    result['mcd64a1'] = [str(t) for t in tifs]
    return result


def get_data_status(bbox=None, start_date=None, end_date=None):
    """
    Show which validation data is available: Rosleshoz zip/CSV, MCD64A1
    cache, Strela files, and (with bbox+period) Strela DB counts.
    """
    status = {
        'ground_truth': {},
        'mcd64a1': {'hdf': [], 'rasters': []},
        'strela_files': {},
        'strela_db': None,
    }

    try:
        z = find_registry_zip()
        status['ground_truth']['rosleshoz_zip'] = z.name
    except ValidationDataError:
        status['ground_truth']['rosleshoz_zip'] = None
    extracted = sorted((GROUND_TRUTH_DIR / 'extracted').glob('*.csv'))
    status['ground_truth']['rosleshoz_csv'] = extracted[-1].name if extracted else None

    status['mcd64a1']['hdf'] = sorted(p.name for p in MCD64A1_DIR.glob('*.hdf'))
    status['mcd64a1']['rasters'] = sorted(p.name for p in MCD64A1_DIR.glob('*.3576.tif'))

    status['strela_files'] = {
        'viirs_detections': (STRELA_DIR / VIIRS_FILE).exists(),
        'burned_areas': (STRELA_DIR / BURNS_FILE).exists(),
    }

    if bbox and start_date and end_date:
        try:
            from django.contrib.gis.geos import Polygon
            from fires.models import BurnArea, FireHotspot
            poly = Polygon.from_bbox(tuple(float(x) for x in bbox))
            start = pd.Timestamp(start_date)
            end = pd.Timestamp(end_date) + timedelta(days=1)
            status['strela_db'] = {
                'fires': FireHotspot.objects.filter(
                    location__within=poly,
                    detected_at__gte=start, detected_at__lt=end).count(),
                'burns': BurnArea.objects.filter(
                    geometry__intersects=poly,
                    pre_date__lte=end.date(), post_date__gte=start.date(),
                    severity__in=BURN_SEVERITIES).count(),
            }
        except Exception as e:
            status['strela_db'] = {'error': str(e)}
    return status


# ---------------------------------------------------------------------------
# Summary for the LLM / API clients
# ---------------------------------------------------------------------------

def build_summary(result):
    s = {
        'success': True,
        'region': result['region_name'] or 'bbox only',
        'period': f"{result['start_date']} .. {result['end_date']}",
        'gt_fires': result['gt_fires'],
        'strela_fires': result['strela_fires'],
        'strela_burn_polygons': result['strela_burn_polygons'],
    }
    d = result.get('detection')
    if d:
        s['detection'] = {
            'precision': round(d['precision'] * 100, 1),
            'recall': round(d['recall'] * 100, 1),
            'f1': round(d['f1'] * 100, 1),
            'tp': d['tp'], 'fp': d['fp'], 'fn': d['fn'],
            'strela_total': d['strela_total'], 'gt_total': d['gt_total'],
            'buffer_m': d['buffer_m'], 'time_window_days': d['time_window_days'],
        }
    b = result.get('burns')
    if b:
        s['burns'] = {
            'iou': b['iou'],
            'burn_f1': b.get('burn_f1'),
            'area_correlation': b['area_correlation'],
            'mean_area_deviation_pct': b['mean_area_deviation_pct'],
            'temporal_lag_days': b['temporal_lag_days'],
            'strela_area_ha': b['strela_area_ha'],
            'modis_area_ha': b['modis_area_ha'],
            'polygons_compared': b['polygons_compared'],
            'time_window_days': b.get('time_window_days'),
            'buffer_m': b.get('buffer_m'),
        }
    if result.get('fp_industrial'):
        s['fp_industrial'] = result['fp_industrial']
    s['artifacts'] = result.get('artifacts', {})
    s['warnings'] = result.get('warnings', [])
    s['message'] = 'Валидация выполнена: ' + (
        'детекция + гари' if (d and b) else 'детекция' if d else 'гари')
    return s