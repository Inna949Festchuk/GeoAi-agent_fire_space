"""
Quantitative validation metrics for the "Strela" system.

Detection  : geodesic buffer (meters) + spatial/temporal join -> Precision/Recall/F1.
Burn scars : raster IoU + Pearson area correlation + temporal lag.

Geospatial rules (fire_monitor_validation_pipeline skill):
- buffers are computed in meters after reprojection to an equal-area CRS,
  never in degrees;
- areas are computed only in an equal-area CRS (EPSG:3576 for Russia);
- temporal windows: +/-3 days (detection), +/-20 days (burn scars —
  MCD64A1 is a monthly composite);
- Strela burn polygons are buffered (VAL_BURN_BUFFER_METERS, default 1 km)
  before raster comparison to compensate dNBR boundary dilution.
"""
import logging
import math
from datetime import datetime, timedelta

import geopandas as gpd
import numpy as np
import pandas as pd
from rasterio.features import rasterize
from rasterio.transform import from_origin

from .config import (
    BURN_TIME_WINDOW_DAYS,
    BURN_BUFFER_METERS,
    BUFFER_METERS,
    DEDUPE_DISTANCE_M,
    DEDUPE_WINDOW_DAYS,
    DETECTION_TIME_WINDOW_DAYS,
    EQUAL_AREA_CRS,
    WGS84_CRS,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def geodesic_buffer(gdf, meters, crs=EQUAL_AREA_CRS):
    """
    Geodesic buffer (real meters): WGS84 -> equal-area CRS -> buffer -> WGS84.
    Row order is preserved (important for index-based joins).
    """
    projected = gdf.to_crs(crs)
    out = projected.copy()
    out['geometry'] = out.geometry.buffer(meters)
    return out.to_crs(WGS84_CRS)


def _py(value):
    """Convert numpy/pandas scalars to JSON-safe Python values."""
    try:
        if value is None or pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (pd.Timestamp, datetime, np.datetime64)):
        return str(value)[:19]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    return value


def _point_samples(gdf, indices, limit=20):
    samples = []
    for i in list(indices)[:limit]:
        geom = gdf.geometry.iloc[i]
        row = gdf.iloc[i]
        entry = {'lon': round(float(geom.x), 5), 'lat': round(float(geom.y), 5)}
        for col in ('detected_at', 'fire_start_date', 'area_ha', 'confidence', 'severity'):
            if col in gdf.columns:
                v = _py(row[col])
                if v is not None:
                    entry[col] = v
        samples.append(entry)
    return samples


def _empty_gdf():
    return gpd.GeoDataFrame(geometry=[], crs='EPSG:4326')


# ---------------------------------------------------------------------------
# Detection: Precision / Recall / F1
# ---------------------------------------------------------------------------

def validate_detection(gt, strela, buffer_m=BUFFER_METERS,
                       time_window_days=DETECTION_TIME_WINDOW_DAYS):
    """
    Compare Strela fire detections (points) with the Rosleshoz registry (points).

    Matching rule: a Strela detection matches a GT fire when it falls inside
    the geodesic buffer (buffer_m) around the GT fire point AND within the
    time window. For deduplicated spots (first_at/last_at columns present)
    the window is measured against the spot's activity period: the fire
    start date may fall anywhere inside [first_at, last_at] (distance 0)
    or within time_window_days of it. Otherwise the window is measured
    against detected_at. Assignment is greedy 1:1 (closest in time, then
    in distance).

    Args:
        gt: GeoDataFrame (WGS84 points) with 'fire_start_date' (datetime)
        strela: GeoDataFrame (WGS84 points) with 'detected_at' (datetime);
            optional 'first_at'/'last_at' for deduplicated fire spots

    Returns dict: tp, fp, fn, precision, recall, f1, totals, samples,
    fp_points/fn_points (GeoDataFrames), warnings.
    """
    warnings = []
    gt = gt.reset_index(drop=True)
    strela = strela.reset_index(drop=True)

    has_time = ('detected_at' in strela.columns
                and pd.to_datetime(strela['detected_at'], errors='coerce').notna().any())
    if not has_time:
        warnings.append('no detection time in Strela data — matching by space only')
    else:
        strela = strela.copy()
        strela['detected_at'] = pd.to_datetime(strela['detected_at'], errors='coerce')

    def _build(tp, fp, fn, fp_idx, fn_idx, extra_warnings=()):
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f = 2 * p * r / (p + r) if (p + r) else 0.0
        return {
            'tp': int(tp), 'fp': int(fp), 'fn': int(fn),
            'precision': round(p, 4), 'recall': round(r, 4), 'f1': round(f, 4),
            'strela_total': int(len(strela)), 'gt_total': int(len(gt)),
            'fp_samples': _point_samples(strela, fp_idx),
            'fn_samples': _point_samples(gt, fn_idx),
            'fp_points': (strela.iloc[fp_idx].reset_index(drop=True)
                          if fp_idx else _empty_gdf()),
            'fn_points': (gt.iloc[fn_idx].reset_index(drop=True)
                          if fn_idx else _empty_gdf()),
            'buffer_m': buffer_m, 'time_window_days': time_window_days,
            'warnings': list(warnings) + list(extra_warnings),
        }

    if gt.empty:
        return _build(0, len(strela), 0,
                      list(range(len(strela))), [],
                      ('Rosleshoz: no registered fires for this bbox/period',))
    if strela.empty:
        return _build(0, 0, len(gt), [], list(range(len(gt))),
                      ('Strela: no fire detections for this bbox/period',))

    gt_buffered = geodesic_buffer(gt, buffer_m).reset_index(drop=True)
    window = timedelta(days=time_window_days)

    # Carry explicit ids through the join: geopandas renames the sjoin index
    # columns across versions (_index/_index_right vs index_left/index_right),
    # and a positional fallback breaks when one GT fire matches many
    # detections (len(joined) > len(gt)).
    strela_j = strela.copy()
    gt_j = gt_buffered.copy()
    strela_j['_strela_id'] = strela.index
    gt_j['_gt_id'] = gt_buffered.index
    joined = gpd.sjoin(strela_j, gt_j, how='inner', predicate='intersects')
    si_list = joined['_strela_id'].tolist()
    gi_list = joined['_gt_id'].tolist()

    # Deduplicated spots carry their activity period: the fire start date
    # matches anywhere inside [first_at, last_at], not only at the first
    # detection (clouds can hide the start; VIIRS usually sees the fire
    # a few days later).
    has_activity = (has_time and 'first_at' in strela.columns
                    and 'last_at' in strela.columns)

    pairs = []
    for si, gi in zip(si_list, gi_list):
        si, gi = int(si), int(gi)
        dt_s = 0.0
        if has_time:
            fire_start = pd.Timestamp(gt.loc[gi, 'fire_start_date'])
            if has_activity:
                first_at = pd.Timestamp(strela.loc[si, 'first_at'])
                last_at = pd.Timestamp(strela.loc[si, 'last_at'])
                if pd.isna(fire_start) or pd.isna(first_at) or pd.isna(last_at):
                    continue
                if fire_start < first_at:
                    dt = first_at - fire_start
                elif fire_start > last_at:
                    dt = fire_start - last_at
                else:
                    dt = pd.Timedelta(0)
            else:
                dt = abs(pd.Timestamp(strela.loc[si, 'detected_at'])
                         - fire_start)
            if pd.isna(dt) or dt > window:
                continue
            dt_s = dt.total_seconds()
        dist = float(strela.geometry.iloc[si].distance(gt_buffered.geometry.iloc[gi]))
        pairs.append((dt_s, dist, si, gi))

    pairs.sort(key=lambda p: (p[0], p[1]))
    used_s, used_g = set(), set()
    for _dt, _dist, si, gi in pairs:
        if si not in used_s and gi not in used_g:
            used_s.add(si)
            used_g.add(gi)

    tp = len(used_s)
    fp = int(len(strela)) - tp
    fn = int(len(gt)) - tp
    fp_idx = [i for i in range(len(strela)) if i not in used_s]
    fn_idx = [i for i in range(len(gt)) if i not in used_g]
    return _build(tp, fp, fn, fp_idx, fn_idx)


# ---------------------------------------------------------------------------
# Detection deduplication: raw per-pass points -> unique fire spots
# ---------------------------------------------------------------------------

def deduplicate_detections(gdf, distance_m=DEDUPE_DISTANCE_M,
                           time_window_days=DEDUPE_WINDOW_DAYS):
    """
    Merge raw per-pass fire detections into unique fire spots.

    Relation: two detections belong to the same spot when they are within
    distance_m of each other AND their detection times differ by at most
    time_window_days; a spot is a connected component under this relation
    (the same rule as detection matching, so one spot is comparable to one
    registered fire). The cluster representative is the EARLIEST detection.

    Args:
        gdf: GeoDataFrame (WGS84 points) with 'detected_at' (datetime).
        distance_m / time_window_days: spot merge rule.

    Returns:
        (deduped GeoDataFrame, info): the deduped frame keeps each spot's
        representative row plus cluster stats (cluster_size, first_at,
        last_at, days_active; frp_max / frp_mean when 'frp' exists);
        info = {'raw', 'clusters', 'distance_m', 'time_window_days'}.
        (gdf unchanged, None) when dedup is not applicable (fewer than 2
        points or no usable detection times).
    """
    n = len(gdf)
    if n < 2 or 'detected_at' not in gdf.columns:
        return gdf, None
    t = pd.to_datetime(gdf['detected_at'], errors='coerce')
    if t.isna().all():
        return gdf, None

    from collections import defaultdict
    from pyproj import Transformer

    transformer = Transformer.from_crs(WGS84_CRS, EQUAL_AREA_CRS, always_xy=True)
    xy = transformer.transform(
        np.asarray(gdf.geometry.x, dtype=float),
        np.asarray(gdf.geometry.y, dtype=float))
    x, y = np.asarray(xy[0], dtype=float), np.asarray(xy[1], dtype=float)

    d = float(distance_m)
    d2 = d * d
    win_s = float(timedelta(days=time_window_days).total_seconds())

    valid = t.notna().to_numpy()
    t0 = t.min()
    ts = np.where(valid, (t - t0).dt.total_seconds().to_numpy(), -1.0)

    # Grid buckets (cell = distance_m): a pair within d can only sit in the
    # same or adjacent cells, so 9 buckets per point is the full candidate
    # set. Buckets are filled in time order; for each new point we only scan
    # the recent tail of each bucket (the time window).
    cell = max(int(d), 1)
    ci = np.floor(x / cell).astype(np.int64)
    cj = np.floor(y / cell).astype(np.int64)

    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb and ra > rb:
            ra, rb = rb, ra
        if ra != rb:
            parent[rb] = ra

    buckets = defaultdict(list)
    for i in np.argsort(ts, kind='stable'):
        if not valid[i]:
            continue  # no time — cannot be merged into a spot
        ti = ts[i]
        ii, jj = int(ci[i]), int(cj[i])
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                lst = buckets.get((ii + di, jj + dj))
                if not lst:
                    continue
                for j in reversed(lst):
                    if ti - ts[j] > win_s:
                        break  # time-ordered: older entries are out of window
                    dx = x[i] - x[j]
                    dy = y[i] - y[j]
                    if dx * dx + dy * dy <= d2:
                        union(int(i), int(j))
        buckets[(ii, jj)].append(int(i))

    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    reps = {}
    for members in groups.values():
        reps[min(members, key=lambda i: (ts[i], i))] = members

    order_reps = sorted(reps)
    deduped = gdf.loc[order_reps].copy().reset_index(drop=True)
    deduped['cluster_size'] = [len(reps[r]) for r in order_reps]
    first_s = np.array([ts[r] for r in order_reps])
    last_s = np.array([max(ts[j] for j in reps[r]) for r in order_reps])
    deduped['first_at'] = t0 + pd.to_timedelta(first_s, unit='s')
    deduped['last_at'] = t0 + pd.to_timedelta(last_s, unit='s')
    deduped['days_active'] = np.round(
        np.clip(last_s - first_s + 86400.0, 86400.0, None) / 86400.0, 1)
    if 'frp' in gdf.columns:
        frp = pd.to_numeric(gdf['frp'], errors='coerce').to_numpy()
        stats = []
        for r in order_reps:
            vals = frp[list(reps[r])]
            vals = vals[~np.isnan(vals)]
            stats.append((float(vals.max()), float(vals.mean())) if vals.size
                         else (float('nan'), float('nan')))
        deduped['frp_max'] = [s[0] for s in stats]
        deduped['frp_mean'] = [s[1] for s in stats]

    return deduped, {
        'raw': int(n),
        'clusters': int(len(order_reps)),
        'distance_m': d,
        'time_window_days': float(time_window_days),
    }


# ---------------------------------------------------------------------------
# Burn scars: IoU / area correlation / temporal lag
# ---------------------------------------------------------------------------

def stitch_mcd64a1(tif_paths, bbox, res_m=500.0, max_pixels=16_000_000):
    """
    Reproject MCD64A1 tile rasters (EPSG:3576 GeoTIFFs: band 1 = burn ordinal
    days, band 2 = valid land mask) onto one common equal-area grid covering
    the bbox.

    Returns (burn_days float32, valid_land bool, transform, res_m_used).
    """
    from pyproj import CRS, Transformer
    from rasterio import open as rio_open
    from rasterio.enums import Resampling
    from rasterio.warp import reproject

    crs = CRS.from_user_input(EQUAL_AREA_CRS)
    transformer = Transformer.from_crs(WGS84_CRS, EQUAL_AREA_CRS, always_xy=True)
    xs, ys = zip(*[
        transformer.transform(lon, lat)
        for lon, lat in [(bbox[0], bbox[1]), (bbox[2], bbox[1]),
                         (bbox[0], bbox[3]), (bbox[2], bbox[3])]
    ])
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)

    def _grid(res):
        gx0 = math.floor(x0 / res) * res
        gy1 = math.ceil(y1 / res) * res
        gy0 = math.floor(y0 / res) * res
        w = int(round((x1 - gx0) / res)) + 1
        h = int(round((gy1 - gy0) / res)) + 1
        return gx0, gy1, w, h

    res = res_m
    gx0, gy1, w, h = _grid(res)
    while w * h > max_pixels and res < 4000:
        res *= 2
        gx0, gy1, w, h = _grid(res)

    transform = from_origin(gx0, gy1, res, res)
    burn = np.zeros((h, w), dtype=np.float32)
    land = np.zeros((h, w), dtype=np.float32)

    for path in tif_paths:
        with rio_open(path) as src:
            src_transform, src_crs = src.transform, src.crs
            days = src.read(1)
            valid = src.read(2)
        reproject(source=days, destination=burn,
                  src_transform=src_transform, src_crs=src_crs,
                  dst_transform=transform, dst_crs=crs,
                  resampling=Resampling.nearest)
        reproject(source=valid, destination=land,
                  src_transform=src_transform, src_crs=src_crs,
                  dst_transform=transform, dst_crs=crs,
                  resampling=Resampling.nearest)

    return burn, land > 0.5, transform, res


def compute_burn_metrics(strela_burns, burn_days, valid_land, transform,
                         period_start, period_end,
                         time_window_days=BURN_TIME_WINDOW_DAYS, res_m=500.0):
    """
    Compare Strela burn polygons (WGS84) with an MCD64A1 burn raster on a
    common equal-area grid.

    Args:
        strela_burns: GeoDataFrame (EPSG:4326 polygons) with 'burn_date'
                      (datetime), optionally 'area_ha', 'severity'
        burn_days: float32 2D — burn ordinal day per pixel (0 = none)
        valid_land: bool 2D
        transform: rasterio transform of the grid (equal-area CRS)
        period_start / period_end: datetime of the analysis period
    """
    warnings = []
    strela = strela_burns.to_crs(EQUAL_AREA_CRS).reset_index(drop=True)
    out_shape = (burn_days.shape[0], burn_days.shape[1])
    n = len(strela)

    # Geodesic buffer (meters, in the equal-area CRS) around Strela
    # polygons — compensates dNBR boundary dilution. Used only for the
    # comparison masks; reported areas stay the original polygon areas.
    strela = strela.copy()
    strela['buffered'] = strela.geometry.buffer(BURN_BUFFER_METERS)

    burn_dates = pd.to_datetime(strela['burn_date'], errors='coerce')
    if burn_dates.isna().any():
        warnings.append(
            f'{int(burn_dates.isna().sum())} Strela burn polygons without a '
            'date — excluded from comparison')
    ordinal_values = np.array(
        [d.toordinal() if pd.notna(d) else 0 for d in burn_dates], dtype='int32')

    window = timedelta(days=time_window_days)
    d_from = (period_start - window).toordinal()
    d_to = (period_end + window).toordinal()
    burned = valid_land & (burn_days >= d_from) & (burn_days <= d_to) & (burn_days > 0)

    # Union Strela mask (buffered); per-pixel value = the polygon burn ordinal day
    all_mask = np.zeros(out_shape, dtype='int32')
    for i in range(n):
        geom = strela['buffered'].iloc[i]
        if geom.is_empty or ordinal_values[i] == 0:
            continue
        tmp = np.zeros(out_shape, dtype='int32')
        m = rasterize([(geom, int(ordinal_values[i]))], out=tmp,
                      transform=transform, fill=0, all_touched=True)
        all_mask = np.where(m > 0, m, all_mask)
    mask_strela = all_mask > 0

    inter = int((mask_strela & burned).sum())
    union = int((mask_strela | burned).sum())
    iou = (inter / union) if union else 0.0

    # Pixel F1: precision — доля пикселей Стрела, где MCD64A1 видит
    # выгорание; recall — доля пикселей MCD64A1, покрытых Стрелой.
    fp_px = int((mask_strela & ~burned).sum())
    fn_px = int((~mask_strela & burned).sum())
    prec = inter / (inter + fp_px) if (inter + fp_px) else 0.0
    rec = inter / (inter + fn_px) if (inter + fn_px) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0

    pixel_ha = (res_m * res_m) / 1e4
    rows = []
    for i in range(n):
        geom = strela['buffered'].iloc[i]
        if geom.is_empty or ordinal_values[i] == 0:
            continue
        tmp = np.zeros(out_shape, dtype='uint8')
        m = rasterize([(geom, 1)], out=tmp, transform=transform, fill=0,
                      all_touched=True) > 0
        if 'area_ha' in strela.columns and pd.notna(strela.loc[i, 'area_ha']):
            area_strela = float(strela.loc[i, 'area_ha'])
        else:
            # area of the ORIGINAL polygon (buffer is comparison-only)
            area_strela = strela.geometry.iloc[i].area / 1e4
        matched = m & burned
        area_modis = float(matched.sum()) * pixel_ha
        lag = (float((burn_days[matched] - ordinal_values[i]).mean())
               if matched.any() else None)
        rows.append({
            'strela_area_ha': round(area_strela, 1),
            'modis_area_ha': round(area_modis, 1),
            'lag_days': round(lag, 2) if lag is not None else None,
        })
    df = pd.DataFrame(rows)

    corr = None
    if len(df) >= 3 and df['strela_area_ha'].std() > 0 and df['modis_area_ha'].std() > 0:
        corr = df['strela_area_ha'].corr(df['modis_area_ha'])
        if corr is None or (isinstance(corr, float) and math.isnan(corr)):
            corr = None

    dev = None
    if len(df):
        with np.errstate(divide='ignore', invalid='ignore'):
            pct = ((df['strela_area_ha'] - df['modis_area_ha']).abs()
                   / df['modis_area_ha'].replace(0, np.nan) * 100)
        if pct.notna().any():
            dev = float(pct.mean())

    lag_days = None
    if len(df):
        lags = df['lag_days'].dropna()
        if len(lags):
            lag_days = float(lags.mean())

    # MCD64A1 burn mask (the reference raster this comparison actually used)
    # as polygons — for the ground-truth sub-layer in the validation map.
    modis_burn = None
    try:
        from rasterio.features import shapes as rio_shapes
        from shapely.geometry import shape as shp
        geoms = [shp(g) for g, _v in rio_shapes(burned.astype('uint8'),
                                                  transform=transform)]
        geoms = [g for g in geoms if not g.is_empty]
        if geoms:
            modis_burn = gpd.GeoDataFrame(geometry=geoms, crs=EQUAL_AREA_CRS)
            modis_burn['area_ha'] = modis_burn.geometry.area / 1e4
            # drop noise fragments below one grid cell x 10
            modis_burn = modis_burn[modis_burn['area_ha'] >= (res_m * res_m) / 1e3]
            modis_burn = modis_burn.to_crs(WGS84_CRS).reset_index(drop=True)
    except Exception:
        logger.exception('MCD64A1 burn-mask vectorization failed')

    return {
        'iou': round(float(iou), 4),
        'burn_f1': round(float(f1), 4),
        'area_correlation': round(float(corr), 4) if corr is not None else None,
        'mean_area_deviation_pct': round(dev, 1) if dev is not None else None,
        'temporal_lag_days': round(lag_days, 2) if lag_days is not None else None,
        'strela_area_ha': round(float(df['strela_area_ha'].sum()), 1) if len(df) else 0.0,
        'modis_area_ha': round(float(df['modis_area_ha'].sum()), 1) if len(df) else 0.0,
        'modis_total_burned_ha': round(float(burned.sum()) * pixel_ha, 1),
        'polygons_compared': len(df),
        'per_polygon': rows[:50],
        'time_window_days': time_window_days,
        'buffer_m': BURN_BUFFER_METERS,
        'resolution_m': res_m,
        'warnings': warnings,
        '_modis_burn': modis_burn,
    }


def validate_burns(strela_burns, tif_paths, bbox, period_start, period_end,
                   time_window_days=BURN_TIME_WINDOW_DAYS, res_m=500.0):
    """Stitch MCD64A1 rasters for the bbox and compute burn scar metrics."""
    if strela_burns is None or strela_burns.empty or not tif_paths:
        return None
    burn_days, valid_land, transform, res_used = stitch_mcd64a1(
        tif_paths, bbox, res_m=res_m)
    if not valid_land.any():
        logger.warning('MCD64A1: no valid land pixels in bbox — burn metrics skipped')
        return None
    return compute_burn_metrics(
        strela_burns, burn_days, valid_land, transform,
        period_start, period_end, time_window_days, res_used)