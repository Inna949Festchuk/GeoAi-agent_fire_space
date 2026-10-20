"""
NASA MCD64A1 (MODIS Burned Area, collection 6.1) client.

Finds monthly sinusoidal tiles via CMR (Common Metadata Repository)
granule search — the legacy LAADS "allData" direct URLs were retired;
the data now lives on LP DAAC earthdatacloud
(https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/...).

Flow per tile:
  1. CMR point query (short_name=MCD64A1 + point at the tile center + month)
     -> granule title + direct download URL (small response, no truncation);
  2. download the .hdf with an Earthdata bearer token (follows the 303
     redirect to a presigned CloudFront URL);
  3. convert BurnDate + QA layers to an equal-area GeoTIFF (EPSG:3576, 500 m):
     band 1 = burn ordinal day (0 = none), band 2 = valid usable land mask
     (QA bit 0 = land, bit 1 = valid; bits 5-7 special conditions excluded).

Everything is cached on disk (HDF + GeoTIFF).
"""
import calendar
import logging
import time
from datetime import date
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import rasterio
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import calculate_default_transform, reproject

from .config import (
    EARTHDATA_TOKEN,
    EQUAL_AREA_CRS,
    MCD64A1_DIR,
    WGS84_CRS,
    ValidationDataError,
)

logger = logging.getLogger(__name__)

CMR_URL = 'https://cmr.earthdata.nasa.gov/search/granules.json'
SINUSOIDAL_CRS = 'ESRI:54008'
# MODIS sinusoidal tile grid constants
_TILESIZE = 1111950.5197
_X_OFF = 20015109.354
_Y_OFF = 10007554.677
BURN_NODATA = 65535
RASTER_SUFFIX = '.3576.tif'
RASTER_RES_M = 500.0
# Politeness delay between CMR calls (CMR limit: 20 req/min)
_CMR_PAUSE_SEC = 1.5


def month_doy(year, month):
    """Day-of-year of the 1st of the month (MCD64A1 monthly naming: 183 for July 2024)."""
    return date(year, month, 1).timetuple().tm_yday


def months_in_period(start, end):
    """All (year, month) pairs overlapping the [start, end] period."""
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return months


def get_modis_tiles(bbox):
    """
    Candidate sinusoidal MODIS tile ids (hXXvYY) covering a WGS84 bbox,
    expanded by one tile in each direction (CMR point queries below confirm
    which tiles actually have a granule for the month).
    """
    transformer = Transformer.from_crs(WGS84_CRS, SINUSOIDAL_CRS, always_xy=True)
    corners = [(bbox[0], bbox[1]), (bbox[2], bbox[1]),
               (bbox[0], bbox[3]), (bbox[2], bbox[3])]
    pts = [transformer.transform(lon, lat) for lon, lat in corners]
    xs, ys = zip(*pts)
    h_min = int((min(xs) + _X_OFF) // _TILESIZE) - 1
    h_max = int((max(xs) + _X_OFF) // _TILESIZE) + 1
    v_min = int((_Y_OFF - max(ys)) // _TILESIZE) - 1
    v_max = int((_Y_OFF - min(ys)) // _TILESIZE) + 1
    tiles = set()
    for h in range(h_min, h_max + 1):
        for v in range(v_min, v_max + 1):
            if 0 <= h <= 35 and 0 <= v <= 17:
                tiles.add(f'h{h:02d}v{v:02d}')
    if not tiles:
        raise ValidationDataError(f'No MODIS sinusoidal tiles found for bbox {bbox}')
    return sorted(tiles)


def tile_center_lonlat(tile):
    """Approximate lon/lat center of a sinusoidal tile (for CMR point queries)."""
    h = int(tile[1:3])
    v = int(tile[4:6])
    x = -_X_OFF + (h + 0.5) * _TILESIZE
    y = _Y_OFF - (v + 0.5) * _TILESIZE
    transformer = Transformer.from_crs(SINUSOIDAL_CRS, WGS84_CRS, always_xy=True)
    return transformer.transform(x, y)


def tile_hdf_pattern(tile, year, month):
    """Glob pattern of the cached HDF name for a tile/month."""
    doy = month_doy(year, month)
    return f'MCD64A1.A{year}{doy:03d}.{tile}.061'


def raster_path_for(hdf_path):
    """GeoTIFF cache path corresponding to an MCD64A1 HDF file."""
    hdf_path = Path(hdf_path)
    return hdf_path.with_name(hdf_path.name[:-4] + RASTER_SUFFIX)


def _month_temporal(year, month):
    last = calendar.monthrange(year, month)[1]
    return f'{year}-{month:02d}-01T00:00:00Z,{year}-{month:02d}-{last}T23:59:59Z'


def _cmr_point_granule(client, lon, lat, year, month):
    """One CMR point query -> (granule_title, direct_hdf_url) or None."""
    params = {
        'short_name': 'MCD64A1',
        'point': f'{lon:.4f},{lat:.4f}',
        'temporal': _month_temporal(year, month),
        'page_size': 1,
    }
    resp = client.get(CMR_URL, params=params)
    if resp.status_code == 429:
        raise ValidationDataError('CMR rate limit exceeded — try again in a minute')
    if resp.status_code != 200:
        raise ValidationDataError(f'CMR query failed: HTTP {resp.status_code}')
    try:
        entries = resp.json().get('feed', {}).get('entry', [])
    except ValueError:
        # Response truncated by a network middlebox — treat as no result
        return None
    if not entries:
        return None
    entry = entries[0]
    title = entry.get('title', '')
    for link in entry.get('links', []):
        href = link.get('href', '')
        if link.get('rel', '').endswith('data#') and href.endswith('.hdf'):
            return title, href
    return None


def find_mcd64a1_granules(bbox, year, month):
    """
    Find all MCD64A1 monthly granules covering the bbox via CMR point
    queries over the candidate tiles. Returns {title: direct_url}.
    """
    tiles = get_modis_tiles(bbox)
    client = httpx.Client(timeout=60)
    granules = {}
    try:
        for tile in tiles:
            lon, lat = tile_center_lonlat(tile)
            try:
                found = _cmr_point_granule(client, lon, lat, year, month)
            except ValidationDataError:
                raise
            except Exception as e:
                logger.warning('CMR point query failed for %s: %s', tile, e)
                continue
            if found:
                granules[found[0]] = found[1]
            time.sleep(_CMR_PAUSE_SEC)
    finally:
        client.close()
    return granules


def _probe_download(url):
    """30-second probe: does the first byte of the file start flowing?"""
    if not EARTHDATA_TOKEN:
        return False
    try:
        with httpx.Client(
            timeout=(15, 30),
            headers={'Authorization': f'Bearer {EARTHDATA_TOKEN}'},
            follow_redirects=True,
        ) as client:
            with client.stream('GET', url) as r:
                r.read(1)
        return True
    except Exception:
        return False


def download_mcd64a1_granule(title, url, out_dir=None):
    """Download one granule (cached). Returns the .hdf path."""
    out_dir = Path(out_dir) if out_dir else MCD64A1_DIR
    if not EARTHDATA_TOKEN:
        raise ValidationDataError(
            'EARTHDATA_TOKEN is not set. Create a token at '
            'https://urs.earthdata.nasa.gov (profile -> Generate Token) and put '
            'it in .env (EARTHDATA_TOKEN=...).'
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    cached = out_dir / f'{title}.hdf'
    if cached.exists():
        logger.info('MCD64A1 %s is already cached', title)
        return cached

    headers = {'Authorization': f'Bearer {EARTHDATA_TOKEN}'}
    with httpx.Client(timeout=(30, 180), headers=headers,
                      follow_redirects=True) as client:
        with client.stream('GET', url) as r:
            if r.status_code in (401, 403):
                raise ValidationDataError(
                    f'NASA download: HTTP {r.status_code} — EARTHDATA_TOKEN '
                    'expired or Terms of Use not accepted. Regenerate the '
                    'token and update .env.'
                )
            r.raise_for_status()
            tmp = out_dir / f'.{title}.hdf.part'
            with open(tmp, 'wb') as f:
                for chunk in r.iter_bytes(chunk_size=1 << 20):
                    f.write(chunk)
            tmp.rename(cached)
    logger.info('MCD64A1 downloaded: %s (%.0f MB)',
                title, cached.stat().st_size / 1e6)
    return cached


def prepare_mcd64a1_raster(hdf_path, out_path=None, res_m=RASTER_RES_M):
    """
    Convert an MCD64A1 HDF tile to an equal-area (EPSG:3576) GeoTIFF.

    Band 1 — burn ordinal day per pixel (float32, 0 = no burn / invalid).
    Band 2 — valid usable land mask (float32 0/1).

    Cached: if the GeoTIFF already exists it is returned as-is.
    """
    import h5py

    hdf_path = Path(hdf_path)
    out_path = Path(out_path) if out_path else raster_path_for(hdf_path)
    if out_path.exists():
        return out_path

    with h5py.File(hdf_path, 'r') as f:
        burn = np.array(f['HDFEOS/GRIB/data/BurnDate'], dtype=np.uint16)
        qa = np.array(f['HDFEOS/GRIB/data/MCD64A1_BrnQA'], dtype=np.uint8)
        grid = np.array(f['HDFEOS/GRIB/GRIBgrid'], dtype=np.float64)

    # GRIBgrid: [x0, y0, y1, x1, x_res, y_res, ...] in sinusoidal meters.
    # Data rows start at the top edge (y1).
    x0, y1 = float(grid[0]), float(grid[2])
    dx = float(grid[4]) if grid[4] else (float(grid[3]) - x0) / burn.shape[1]
    dy = float(grid[5]) if grid[5] else (y1 - float(grid[1])) / burn.shape[0]
    src_transform = from_origin(x0, y1, dx, dy)
    src_crs = CRS.from_user_input(SINUSOIDAL_CRS)

    # QA bitmask: bit 0 = land, bit 1 = valid; bits 5-7 = special conditions
    # (persistent hotspots etc.) — excluded from the main count.
    is_land = (qa & 0b1) > 0
    is_valid = (qa & 0b10) > 0
    special = (qa & 0b11100000) != 0
    usable = is_land & is_valid & ~special

    days = _burn_date_ordinal_days(burn)
    days = np.where(usable, days, 0.0).astype(np.float32)
    land_mask = usable.astype(np.float32)

    dst_crs = CRS.from_user_input(EQUAL_AREA_CRS)
    dst_transform, dst_w, dst_h = calculate_default_transform(
        src_crs, dst_crs, burn.shape[1], burn.shape[0],
        *src_transform.bounds, resolution=res_m)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        str(out_path), 'w', driver='GTiff', width=dst_w, height=dst_h, count=2,
        dtype='float32', crs=dst_crs, transform=dst_transform, compress='deflate',
    ) as dst:
        reproject(source=days, destination=rasterio.band(dst, 1),
                  src_transform=src_transform, src_crs=src_crs,
                  dst_transform=dst_transform, dst_crs=dst_crs,
                  resampling=Resampling.nearest)
        reproject(source=land_mask, destination=rasterio.band(dst, 2),
                  src_transform=src_transform, src_crs=src_crs,
                  dst_transform=dst_transform, dst_crs=dst_crs,
                  resampling=Resampling.nearest)
    logger.info('MCD64A1 raster prepared: %s (%dx%d @ %.0f m)',
                out_path.name, dst_w, dst_h, res_m)
    return out_path


def _burn_date_ordinal_days(burn):
    """uint16 BurnDate (year*1000 + day-of-year, 65535 = missing) -> ordinal day."""
    out = np.zeros(burn.shape, dtype=np.float32)
    valid = burn != BURN_NODATA
    if not valid.any():
        return out
    years = (burn[valid] // 1000).astype(np.int64)
    doys = (burn[valid] % 1000).astype(np.int64)
    dt = pd.to_datetime(years.astype(str) + '-' + doys.astype(str), format='%Y-%j')
    # ns since epoch -> days
    out[valid] = (dt.to_numpy().astype('int64') // 10**9).astype('int64') // 86400
    return out


def prepare_mcd64a1(bbox, start, end, out_dir=None):
    """
    Ensure MCD64A1 rasters for all months overlapping [start, end] and all
    tiles covering the bbox. Local cache first (HDFs already on disk, e.g.
    pre-downloaded from the host via validation/downnasagov.py — no network
    needed); otherwise finds granules via CMR and downloads them.

    Returns (list of GeoTIFF paths, list of warnings).
    Raises ValidationDataError when no raster is available at all.
    """
    out_dir = Path(out_dir) if out_dir else MCD64A1_DIR
    tifs, warnings = [], []
    for (y, m) in months_in_period(start, end):
        month_pat = f'MCD64A1.A{y}{month_doy(y, m):03d}'
        # Cache first: convert HDFs on disk, plus accept rasters prepared
        # earlier or provided locally (same naming convention: <title>.3576.tif)
        rasters = sorted(out_dir.glob(f'{month_pat}.*.3576.tif'))
        for hdf in sorted(out_dir.glob(f'{month_pat}.*.hdf')):
            if raster_path_for(hdf) in rasters:
                continue  # already converted
            try:
                rasters.append(prepare_mcd64a1_raster(hdf))
            except Exception as e:
                logger.exception('MCD64A1 raster conversion failed for %s', hdf)
                warnings.append(f'MCD64A1 {hdf.name}: {e}')
        if rasters:
            tifs.extend(rasters)
            continue

        try:
            granules = find_mcd64a1_granules(bbox, y, m)
        except ValidationDataError as e:
            warnings.append(f'MCD64A1 {y}-{m:02d}: {e}')
            continue
        if not granules:
            warnings.append(f'MCD64A1 {y}-{m:02d}: no granules found over bbox in CMR')
            continue
        # Quick network probe: if the first file does not start flowing within
        # 30 s, skip all downloads for this month (the request must not hang).
        first_url = next(iter(granules.values()))
        if not _probe_download(first_url):
            warnings.append(
                f'MCD64A1 {y}-{m:02d}: download from NASA stalled (network). '
                'Retry later, or fetch the files from another network (e.g. VPN) '
                'with validation/downnasagov.py into data/mcd64a1/.')
            continue
        consecutive_failures = 0
        for title, url in granules.items():
            if consecutive_failures >= 2:
                warnings.append(
                    'MCD64A1: aborted early — 2 consecutive download failures '
                    '(network to NASA seems unavailable right now)')
                break
            try:
                hdf = download_mcd64a1_granule(title, url, out_dir=out_dir)
                tifs.append(prepare_mcd64a1_raster(hdf))
                consecutive_failures = 0
            except ValidationDataError as e:
                warnings.append(f'MCD64A1 {title}: {e}')
                consecutive_failures += 1
            except Exception as e:
                logger.exception('MCD64A1 prepare failed for %s', title)
                warnings.append(f'MCD64A1 {title}: {e}')
                consecutive_failures += 1
    if not tifs:
        raise ValidationDataError(
            'MCD64A1 data is unavailable (no granule could be downloaded/converted). '
            'Download the tiles from the host with validation/downnasagov.py, '
            'then retry. ' + ' | '.join(warnings[:3])
        )
    return tifs, warnings


def cached_mcd64a1_rasters(bbox, start, end, out_dir=None):
    """Existing MCD64A1 GeoTIFFs for the period without any download."""
    out_dir = Path(out_dir) if out_dir else MCD64A1_DIR
    if not out_dir.exists():
        return []
    tifs = []
    for (y, m) in months_in_period(start, end):
        for tif in sorted(out_dir.glob(f'MCD64A1.A{y}{month_doy(y, m):03d}.*.3576.tif')):
            if tif not in tifs:
                tifs.append(tif)
    return tifs