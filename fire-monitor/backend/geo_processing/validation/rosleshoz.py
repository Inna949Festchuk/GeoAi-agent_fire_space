"""
Rosleshoz fire registry loader (form 1T-ISM, tochno.st dump).

The registry ships as a zip with a large CSV (~500 MB, sep=';').
The CSV is extracted once into a cache directory, read with only the
needed columns, filtered by bbox + period, and the filtered slice is
cached so repeated runs do not re-read the giant file.
"""
import hashlib
import logging
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd

from .config import GROUND_TRUTH_DIR, ValidationDataError

logger = logging.getLogger(__name__)

ZIP_GLOB = 'data_fires_*1T*_csv.zip'
CSV_GLOB = 'data_fires_*1T*.csv'
DATE_FMT = '%d.%m.%Y'

# Columns of the 1T-ISM registry actually needed for validation
_NEEDED_COLUMNS = {
    'region': 'region',
    'latitude': 'latitude',
    'longitude': 'longitude',
    'date_beginning': 'fire_start_date',
    'date_end': 'fire_end_date',
    'area_total': 'area_ha',
    'area_forest': 'area_forest_ha',
    'type': 'fire_type',
    'code': 'fire_code',
    'current_state': 'current_state',
    'zone': 'zone',
}


def find_registry_zip(data_dir=GROUND_TRUTH_DIR):
    """Find the Rosleshoz 1T-ISM zip in the ground truth directory."""
    zips = sorted(Path(data_dir).glob(ZIP_GLOB))
    if not zips:
        raise ValidationDataError(
            f'Rosleshoz 1T-ISM zip not found in {data_dir} '
            f'(expected a file matching {ZIP_GLOB}). Download the registry from '
            'https://tochno.st/datasets/fires and put the zip there.'
        )
    return zips[-1]


def ensure_extracted_csv(data_dir=GROUND_TRUTH_DIR):
    """Extract the registry CSV from its zip (once). Returns the CSV path."""
    data_dir = Path(data_dir)
    extracted_dir = data_dir / 'extracted'
    csv_files = sorted(extracted_dir.glob(CSV_GLOB))
    if csv_files:
        return csv_files[0]

    zip_path = find_registry_zip(data_dir)
    extracted_dir.mkdir(parents=True, exist_ok=True)
    logger.info('Extracting Rosleshoz registry from %s ...', zip_path.name)
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith('.csv')]
        if not names:
            raise ValidationDataError(f'No CSV file inside {zip_path.name}')
        zf.extract(names[0], extracted_dir)
    return extracted_dir / Path(names[0]).name


def _cache_path(bbox, start_date, end_date, data_dir):
    key = f'{bbox[0]:.3f}_{bbox[1]:.3f}_{bbox[2]:.3f}_{bbox[3]:.3f}_{start_date}_{end_date}'
    digest = hashlib.md5(key.encode('utf-8')).hexdigest()[:10]
    return Path(data_dir) / 'cache' / f'1t_{digest}.csv'


def load_rosleshoz(bbox, start_date, end_date, data_dir=GROUND_TRUTH_DIR):
    """
    Load the Rosleshoz 1T-ISM registry filtered by bbox and period.

    Args:
        bbox: [minx, miny, maxx, maxy] in WGS84
        start_date / end_date: 'YYYY-MM-DD' — fire start date within the period
        data_dir: ground truth directory (for tests / custom layout)

    Returns:
        GeoDataFrame (EPSG:4326 points) with columns: fire_start_date,
        fire_end_date, area_ha, fire_type, region, fire_code, current_state, zone.
    """
    bbox = tuple(float(x) for x in bbox)
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    data_dir = Path(data_dir)

    cache_file = _cache_path(bbox, start_date, end_date, data_dir)
    if cache_file.exists():
        df = pd.read_csv(cache_file, parse_dates=['fire_start_date', 'fire_end_date'])
        return _to_geodataframe(df)

    csv_path = ensure_extracted_csv(data_dir)
    logger.info('Reading Rosleshoz registry %s (%.0f MB) ...',
                csv_path.name, csv_path.stat().st_size / 1e6)
    # Chunked read: the full CSV is ~500 MB (a one-shot read peaks at 2-3 GB
    # of RAM and OOMs the 4 g backend container). Pre-filtering by bbox in
    # each chunk keeps peak RAM well below 1 GB.
    parts = []
    for chunk in pd.read_csv(
        csv_path, sep=';', encoding='utf-8', low_memory=False,
        usecols=list(_NEEDED_COLUMNS.keys()), chunksize=200_000,
    ):
        lat = pd.to_numeric(chunk['latitude'], errors='coerce')
        lon = pd.to_numeric(chunk['longitude'], errors='coerce')
        parts.append(chunk[
            (lat >= bbox[1]) & (lat <= bbox[3])
            & (lon >= bbox[0]) & (lon <= bbox[2])
        ])
    df = pd.concat(parts, ignore_index=True)
    df = df.rename(columns=_NEEDED_COLUMNS)

    df['latitude'] = pd.to_numeric(df['latitude'], errors='coerce')
    df['longitude'] = pd.to_numeric(df['longitude'], errors='coerce')
    df['area_ha'] = pd.to_numeric(df['area_ha'], errors='coerce')
    df = df.dropna(subset=['latitude', 'longitude'])

    df['fire_start_date'] = pd.to_datetime(
        df['fire_start_date'], format=DATE_FMT, dayfirst=True, errors='coerce')
    df['fire_end_date'] = pd.to_datetime(
        df['fire_end_date'], format=DATE_FMT, dayfirst=True, errors='coerce')
    df = df.dropna(subset=['fire_start_date'])

    # Period: fire started within [start, end]
    df = df[(df['fire_start_date'] >= start) & (df['fire_start_date'] <= end)]

    # bbox (fast numeric bounds before building geometry)
    df = df[
        (df['longitude'] >= bbox[0]) & (df['longitude'] <= bbox[2])
        & (df['latitude'] >= bbox[1]) & (df['latitude'] <= bbox[3])
    ]
    df = df.reset_index(drop=True)
    logger.info('Rosleshoz registry: %d fires for bbox=%s %s..%s',
                len(df), bbox, start.date(), end.date())

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache_file, index=False)
    return _to_geodataframe(df)


def _to_geodataframe(df):
    if df.empty:
        return gpd.GeoDataFrame(
            df,
            geometry=gpd.points_from_xy(
                pd.Series(dtype=float), pd.Series(dtype=float)),
            crs='EPSG:4326',
        )
    return gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df['longitude'], df['latitude']),
        crs='EPSG:4326',
    )