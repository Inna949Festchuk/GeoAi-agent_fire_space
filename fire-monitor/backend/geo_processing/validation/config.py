"""
Validation pipeline configuration.

All paths and tunable parameters come from environment variables with
sane defaults, so the same code works in dev (mounted ./data, ./reports)
and prod without code changes.
"""
import os
from pathlib import Path

# Base directories (Docker: fire-monitor/data -> /data, fire-monitor/reports -> /reports)
VAL_DATA_DIR = Path(os.environ.get('VAL_DATA_DIR', '/data'))
VAL_REPORTS_DIR = Path(os.environ.get('VAL_REPORTS_DIR', '/reports'))

GROUND_TRUTH_DIR = VAL_DATA_DIR / 'ground_truth'
MCD64A1_DIR = VAL_DATA_DIR / 'mcd64a1'
STRELA_DIR = VAL_DATA_DIR / 'strela'

# NASA Earthdata token — from .env / compose environment. Never hardcode.
EARTHDATA_TOKEN = os.environ.get('EARTHDATA_TOKEN', '').strip()

# --- Validation parameters (fire_monitor_validation_pipeline skill) ---
BUFFER_METERS = float(os.environ.get('VAL_BUFFER_METERS', '500'))
DETECTION_TIME_WINDOW_DAYS = int(os.environ.get('VAL_DETECTION_WINDOW_DAYS', '3'))
# Detection dedup: raw per-pass VIIRS/MODIS detections re-detect the same
# spot on every orbit (~2 passes/day), so before matching they are merged
# into unique fire spots under the same rule as matching (distance + time
# window). Defaults follow BUFFER_METERS / DETECTION_TIME_WINDOW_DAYS.
DEDUPE_DEFAULT = os.environ.get('VAL_DEDUPE', '1').strip().lower() in (
    '1', 'true', 'yes', 'on')
DEDUPE_DISTANCE_M = float(os.environ.get('VAL_DEDUPE_DISTANCE_M',
                                         str(BUFFER_METERS)))
DEDUPE_WINDOW_DAYS = float(os.environ.get('VAL_DEDUPE_WINDOW_DAYS',
                                          str(DETECTION_TIME_WINDOW_DAYS)))
# MCD64A1 is a monthly composite — the burn date can differ by 2-3 weeks from
# the real mapping date, so the window is wider than for detection (3 days).
BURN_TIME_WINDOW_DAYS = int(os.environ.get('VAL_BURN_WINDOW_DAYS', '20'))
# Geodesic buffer (meters) applied to Strela burn polygons before comparison
# with the MCD64A1 raster — compensates dNBR boundary dilution.
BURN_BUFFER_METERS = float(os.environ.get('VAL_BURN_BUFFER_METERS', '1000'))
# Severity classes loaded for burn comparison (low is filtered out — it is
# mostly phenology / false dNBR, not real fires).
BURN_SEVERITIES = [s.strip() for s in os.environ.get(
    'VAL_BURN_SEVERITIES', 'moderate,high').split(',') if s.strip()]
# Minimum burn polygon area (ha) — tiny polygons are noise, not fires.
MIN_BURN_AREA_HA = float(os.environ.get('VAL_MIN_BURN_AREA_HA', '500'))
# Equal-area CRS for geodesic buffers and area computation (Russia-friendly)
EQUAL_AREA_CRS = os.environ.get('VAL_EQUAL_AREA_CRS', 'EPSG:3576')
WGS84_CRS = 'EPSG:4326'

# --- MCD64A1 (MODIS Burned Area, collection 6.1) ---
MCD64A1_COLLECTION = os.environ.get('MCD64A1_COLLECTION', '61')
MCD64A1_BASE_URL = (
    f'https://ladsweb.modaps.eosdis.nasa.gov/archive/allData/'
    f'{MCD64A1_COLLECTION}/MCD64A1'
)

# Target metric values (stakeholder requirements, hackathon "Хак ЛЦТ 24")
TARGETS = {
    'precision': 0.80,
    'recall': 0.75,
    'f1': 0.77,
    'burn_iou': 0.60,
    'burn_area_correlation': 0.80,
    'burn_mean_area_deviation_pct': 20.0,
    'burn_f1': 0.50,
}


class ValidationDataError(Exception):
    """Validation input data is missing or unusable.

    The message must tell the user exactly what to fix: which file to put
    where, or which token to regenerate.
    """