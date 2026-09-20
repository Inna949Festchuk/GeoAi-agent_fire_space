"""
Burn severity calculation using Normalized Burn Ratio (NBR).

NBR = (NIR - SWIR2) / (NIR + SWIR2)
dNBR = NBR_pre - NBR_post

Severity classification (USGS standard):
- dNBR < -100: Fire-induced enhancement (unburned/green)
- -100 <= dNBR < 100: Unburned
- 100 <= dNBR < 270: Low severity
- 270 <= dNBR < 440: Moderate-low severity
- 440 <= dNBR < 660: Moderate-high severity
- dNBR >= 660: High severity
"""

import logging
import numpy as np
import rasterio
from rasterio.windows import from_bounds
from rasterio.warp import calculate_default_transform, reproject, Resampling, transform_bounds
from rasterio.io import MemoryFile

logger = logging.getLogger(__name__)

# Severity thresholds (dNBR * 1000, as values are typically scaled)
SEVERITY_THRESHOLDS = {
    'enhancement': (-np.inf, -100),
    'unburned': (-100, 100),
    'low': (100, 270),
    'moderate_low': (270, 440),
    'moderate_high': (440, 660),
    'high': (660, np.inf),
}

# Simplified 4-class mapping
SEVERITY_4CLASS = {
    'unburned': (-np.inf, 100),
    'low': (100, 270),
    'moderate': (270, 440),
    'high': (440, np.inf),
}


def calculate_nbr(nir_band, swir2_band):
    """
    Calculate Normalized Burn Ratio.

    NBR = (NIR - SWIR2) / (NIR + SWIR2)

    For Sentinel-2: NIR = B08, SWIR2 = B12
    For Landsat 8/9: NIR = B5, SWIR2 = B7

    Handles NaN values (e.g., from cloud masking) by propagating them.

    Args:
        nir_band: numpy array of NIR reflectance (may contain NaN)
        swir2_band: numpy array of SWIR2 reflectance (may contain NaN)

    Returns:
        numpy array of NBR values (-1 to 1), NaN where input was NaN
    """
    nir = nir_band.astype(float)
    swir2 = swir2_band.astype(float)

    denominator = nir + swir2
    # Use np.where to handle NaN properly
    nbr = np.where(
        (denominator != 0) & ~np.isnan(denominator),
        (nir - swir2) / denominator,
        np.nan
    )

    return np.clip(nbr, -1, 1)


def calculate_dnbr(nbr_pre, nbr_post):
    """
    Calculate delta NBR (burn severity index).

    dNBR = NBR_pre - NBR_post
    Higher values = more severe burn

    Handles NaN values by propagating them.

    Args:
        nbr_pre: numpy array of pre-fire NBR (may contain NaN)
        nbr_post: numpy array of post-fire NBR (may contain NaN)

    Returns:
        numpy array of dNBR values (scaled by 1000 for integer representation),
        NaN where either input was NaN
    """
    dnbr = (nbr_pre - nbr_post) * 1000
    return dnbr


def classify_severity(dnbr_array, n_classes=4):
    """
    Classify dNBR values into severity classes.

    NaN values are classified as 'unburned' (masked out).

    Args:
        dnbr_array: numpy array of dNBR values (scaled by 1000, may contain NaN)
        n_classes: Number of severity classes (4 or 6)

    Returns:
        numpy array of severity class labels
    """
    thresholds = SEVERITY_4CLASS if n_classes == 4 else SEVERITY_THRESHOLDS

    severity = np.full(dnbr_array.shape, 'unburned', dtype=object)

    for class_name, (low, high) in thresholds.items():
        mask = (dnbr_array >= low) & (dnbr_array < high) & ~np.isnan(dnbr_array)
        severity[mask] = class_name

    return severity


def calculate_burn_area_from_arrays(
    nbr_pre_array,
    nbr_post_array,
    transform,
    crs,
    n_classes=4,
    min_area_ha=1.0,
):
    """
    Calculate burn area statistics from NBR arrays.

    Handles NaN values (e.g., from cloud masking) by ignoring them in statistics.

    Args:
        nbr_pre_array: Pre-fire NBR array (may contain NaN)
        nbr_post_array: Post-fire NBR array (may contain NaN)
        transform: Rasterio affine transform
        crs: Coordinate reference system
        n_classes: Number of severity classes
        min_area_ha: Minimum burn area to include (hectares)

    Returns:
        Dict with burn area statistics by severity class
    """
    dnbr = calculate_dnbr(nbr_pre_array, nbr_post_array)
    severity = classify_severity(dnbr, n_classes)

    pixel_area = abs(transform.a * transform.e)
    pixel_area_ha = pixel_area / 10000

    stats = {}
    for class_name in (SEVERITY_4CLASS if n_classes == 4 else SEVERITY_THRESHOLDS):
        mask = severity == class_name
        pixel_count = np.sum(mask)
        area_ha = pixel_count * pixel_area_ha

        # Use nanmean to ignore NaN values in statistics
        mean_dnbr = float(np.nanmean(dnbr[mask])) if pixel_count > 0 else 0

        stats[class_name] = {
            'pixel_count': int(pixel_count),
            'area_ha': round(float(area_ha), 2),
            'mean_dnbr': round(mean_dnbr, 1) if not np.isnan(mean_dnbr) else 0,
        }

    stats['total_burned_ha'] = round(
        sum(s['area_ha'] for k, s in stats.items() if k != 'unburned'),
        2,
    )

    return stats


def apply_cloud_mask(band, scl, cloud_threshold=8):
    """
    Apply cloud mask to a spectral band using Scene Classification Layer.

    Masks pixels where SCL indicates clouds (values >= 8):
    - 8: Cloud medium probability
    - 9: Cloud high probability
    - 10: Thin cirrus
    - 11: Snow (optional, can be included)

    Args:
        band: numpy array of spectral band values
        scl: numpy array of SCL values (same shape as band)
        cloud_threshold: Minimum SCL value to consider as cloud (default: 8)

    Returns:
        numpy array with cloud pixels set to NaN
    """
    masked = band.copy().astype(float)

    # Create cloud mask (SCL >= 8 indicates clouds/cirrus)
    cloud_mask = scl >= cloud_threshold

    # Set cloud pixels to NaN
    masked[cloud_mask] = np.nan

    cloud_pixels = np.sum(cloud_mask)
    total_pixels = scl.size
    cloud_percentage = (cloud_pixels / total_pixels) * 100

    logger.info(f'Cloud masking: {cloud_pixels}/{total_pixels} pixels masked ({cloud_percentage:.1f}%)')

    return masked


def process_sentinel2_burn(
    pre_assets,
    post_assets,
    bbox=None,
    n_classes=4,
    pre_scl_url=None,
    post_scl_url=None,
    apply_cloud_masking=True,
):
    """
    Process burn severity from Sentinel-2 assets.

    Args:
        pre_assets: Dict with band -> URL for pre-fire image
        post_assets: Dict with band -> URL for post-fire image
        bbox: Optional bounding box to clip (minx, miny, maxx, maxy)
        n_classes: Number of severity classes
        pre_scl_url: Optional URL to pre-fire SCL band for cloud masking
        post_scl_url: Optional URL to post-fire SCL band for cloud masking
        apply_cloud_masking: Whether to apply cloud masking if SCL available (default: True)

    Returns:
        Dict with burn statistics and metadata
    """
    required_bands = ['B08', 'B12']

    # Validate bbox size to prevent OOM
    if bbox:
        lon_span = bbox[2] - bbox[0]
        lat_span = bbox[3] - bbox[1]
        max_span = 30  # Maximum 30 degrees (~3000km × 3000km)
        
        if lon_span > max_span or lat_span > max_span:
            raise ValueError(
                f'Bbox too large ({lon_span:.1f}° × {lat_span:.1f}°). '
                f'Maximum size is {max_span}° × {max_span}°. '
                f'Please specify a smaller region.'
            )

    print(f"[DEBUG burn_severity] Processing with bbox={bbox}")
    print(f"[DEBUG burn_severity] Pre assets: {list(pre_assets.keys())}")
    print(f"[DEBUG burn_severity] Post assets: {list(post_assets.keys())}")

    for assets, label in [(pre_assets, 'pre'), (post_assets, 'post')]:
        for band in required_bands:
            if band not in assets:
                raise ValueError(f'Missing {band} in {label}-fire assets')

    results = {}
    for label, assets in [('pre', pre_assets), ('post', post_assets)]:
        print(f"[DEBUG burn_severity] Opening {label}-fire assets...")
        print(f"[DEBUG burn_severity] B08 URL: {assets['B08'][:100]}...")

        # Determine SCL URL for this scene
        scl_url = pre_scl_url if label == 'pre' else post_scl_url
        use_cloud_masking = apply_cloud_masking and scl_url is not None

        if use_cloud_masking:
            print(f"[DEBUG burn_severity] Cloud masking enabled for {label}-fire scene")

        with rasterio.open(assets['B08']) as src_nir, \
             rasterio.open(assets['B12']) as src_swir:

            print(f"[DEBUG burn_severity] {label} NIR shape: {src_nir.shape}, CRS: {src_nir.crs}")
            print(f"[DEBUG burn_severity] {label} SWIR shape: {src_swir.shape}, CRS: {src_swir.crs}")

            if bbox:
                print(f"[DEBUG burn_severity] Clipping to bbox: {bbox}")
                # Reproject bbox from WGS84 (EPSG:4326) to image CRS
                bbox_utm = transform_bounds('EPSG:4326', src_nir.crs, *bbox)
                print(f"[DEBUG burn_severity] Reprojected bbox to {src_nir.crs}: {bbox_utm}")

                # Read NIR band (10m resolution) with window
                window_nir = from_bounds(*bbox_utm, transform=src_nir.transform)

                # Clamp window to image bounds
                window_nir = window_nir.intersection(
                    rasterio.windows.Window(0, 0, src_nir.width, src_nir.height)
                )

                # Calculate target size and determine if downsampling is needed
                target_width = int(window_nir.width)
                target_height = int(window_nir.height)
                max_dimension = 3000  # Limit to 3000x3000 pixels max

                if target_width > max_dimension or target_height > max_dimension:
                    # Calculate downsample factor
                    downsample = max(target_width // max_dimension, target_height // max_dimension)
                    print(f"[DEBUG burn_severity] Downsampling by factor {downsample} (original: {target_width}x{target_height})")

                    # Read with downsampling
                    out_shape = (target_height // downsample, target_width // downsample)
                    nir = src_nir.read(1, window=window_nir, out_shape=out_shape, resampling=rasterio.enums.Resampling.average).astype(float) / 10000

                    # Read SWIR2 with same downsampling
                    window_swir = from_bounds(*bbox_utm, transform=src_swir.transform)
                    window_swir = window_swir.intersection(
                        rasterio.windows.Window(0, 0, src_swir.width, src_swir.height)
                    )
                    swir2_out_shape = (out_shape[0] // 2, out_shape[1] // 2)  # SWIR2 is 20m, so half the size
                    swir2 = src_swir.read(1, window=window_swir, out_shape=swir2_out_shape, resampling=rasterio.enums.Resampling.average).astype(float) / 10000

                    # Resize SWIR2 to match NIR
                    swir2_up = np.repeat(np.repeat(swir2, 2, axis=0), 2, axis=1)
                    min_h = min(swir2_up.shape[0], nir.shape[0])
                    min_w = min(swir2_up.shape[1], nir.shape[1])
                    swir2 = swir2_up[:min_h, :min_w]
                    nir = nir[:min_h, :min_w]

                    # Read and resize SCL if cloud masking enabled
                    scl = None
                    if use_cloud_masking:
                        try:
                            with rasterio.open(scl_url) as src_scl:
                                window_scl = from_bounds(*bbox_utm, transform=src_scl.transform)
                                window_scl = window_scl.intersection(
                                    rasterio.windows.Window(0, 0, src_scl.width, src_scl.height)
                                )
                                # SCL is 20m resolution, same as SWIR2
                                scl = src_scl.read(1, window=window_scl, out_shape=swir2_out_shape, resampling=rasterio.enums.Resampling.nearest)
                                # Resize to match NIR
                                scl_up = np.repeat(np.repeat(scl, 2, axis=0), 2, axis=1)
                                scl = scl_up[:min_h, :min_w]
                                print(f"[DEBUG burn_severity] SCL loaded for cloud masking")
                        except Exception as e:
                            logger.warning(f'Failed to load SCL for {label}-fire: {e}')
                            use_cloud_masking = False

                    # Adjust transform for downsampled data
                    transform = src_nir.window_transform(window_nir)
                    transform = transform * transform.scale(downsample, downsample)
                else:
                    # No downsampling needed, read at full resolution
                    nir = src_nir.read(1, window=window_nir).astype(float) / 10000

                    # Read SWIR2 band (20m resolution) with window
                    window_swir = from_bounds(*bbox_utm, transform=src_swir.transform)
                    window_swir = window_swir.intersection(
                        rasterio.windows.Window(0, 0, src_swir.width, src_swir.height)
                    )
                    swir2 = src_swir.read(1, window=window_swir).astype(float) / 10000

                    # Resize SWIR2 to match NIR
                    swir2_up = np.repeat(np.repeat(swir2, 2, axis=0), 2, axis=1)
                    min_h = min(swir2_up.shape[0], nir.shape[0])
                    min_w = min(swir2_up.shape[1], nir.shape[1])
                    swir2 = swir2_up[:min_h, :min_w]
                    nir = nir[:min_h, :min_w]

                    # Read and resize SCL if cloud masking enabled
                    scl = None
                    if use_cloud_masking:
                        try:
                            with rasterio.open(scl_url) as src_scl:
                                window_scl = from_bounds(*bbox_utm, transform=src_scl.transform)
                                window_scl = window_scl.intersection(
                                    rasterio.windows.Window(0, 0, src_scl.width, src_scl.height)
                                )
                                scl = src_scl.read(1, window=window_scl).astype(float) / 10000
                                # Resize to match NIR
                                scl_up = np.repeat(np.repeat(scl, 2, axis=0), 2, axis=1)
                                scl = scl_up[:min_h, :min_w]
                                print(f"[DEBUG burn_severity] SCL loaded for cloud masking")
                        except Exception as e:
                            logger.warning(f'Failed to load SCL for {label}-fire: {e}')
                            use_cloud_masking = False

                    transform = src_nir.window_transform(window_nir)
                
                print(f"[DEBUG burn_severity] Final NIR shape: {nir.shape}, SWIR shape: {swir2.shape}")
            else:
                # Read full image - need to resample SWIR2 to match NIR resolution
                nir = src_nir.read(1).astype(float) / 10000
                swir2_full = src_swir.read(1).astype(float) / 10000
                # Double SWIR2 resolution to match NIR (20m -> 10m)
                swir2 = np.repeat(np.repeat(swir2_full, 2, axis=0), 2, axis=1)
                # Crop to match NIR exactly
                swir2 = swir2[:nir.shape[0], :nir.shape[1]]
                transform = src_nir.transform

                # Read SCL if cloud masking enabled
                scl = None
                if use_cloud_masking:
                    try:
                        with rasterio.open(scl_url) as src_scl:
                            scl_full = src_scl.read(1)
                            # Resize to match NIR
                            scl = np.repeat(np.repeat(scl_full, 2, axis=0), 2, axis=1)
                            scl = scl[:nir.shape[0], :nir.shape[1]]
                            print(f"[DEBUG burn_severity] SCL loaded for cloud masking (full image)")
                    except Exception as e:
                        logger.warning(f'Failed to load SCL for {label}-fire: {e}')
                        use_cloud_masking = False

            # Apply cloud masking if enabled and SCL available
            if use_cloud_masking and scl is not None:
                print(f"[DEBUG burn_severity] Applying cloud mask to {label}-fire scene...")
                nir = apply_cloud_mask(nir, scl)
                swir2 = apply_cloud_mask(swir2, scl)

            nbr = calculate_nbr(nir, swir2)
            print(f"[DEBUG burn_severity] {label} NBR stats: min={np.nanmin(nbr):.3f}, max={np.nanmax(nbr):.3f}, mean={np.nanmean(nbr):.3f}")

            results[label] = {
                'nbr': nbr,
                'transform': transform,
                'crs': src_nir.crs,
            }

    # Ensure pre and post have the same shape
    pre_nbr = results['pre']['nbr']
    post_nbr = results['post']['nbr']
    
    if pre_nbr.shape != post_nbr.shape:
        print(f"[DEBUG burn_severity] Shape mismatch: pre={pre_nbr.shape}, post={post_nbr.shape}")
        # Crop both to minimum dimensions
        min_h = min(pre_nbr.shape[0], post_nbr.shape[0])
        min_w = min(pre_nbr.shape[1], post_nbr.shape[1])
        pre_nbr = pre_nbr[:min_h, :min_w]
        post_nbr = post_nbr[:min_h, :min_w]
        print(f"[DEBUG burn_severity] Cropped to: pre={pre_nbr.shape}, post={post_nbr.shape}")
    
    dnbr = calculate_dnbr(pre_nbr, post_nbr)
    print(f"[DEBUG burn_severity] dNBR stats: min={dnbr.min():.1f}, max={dnbr.max():.1f}, mean={dnbr.mean():.1f}")
    
    severity = classify_severity(dnbr, n_classes)
    print(f"[DEBUG burn_severity] Severity classes: {np.unique(severity, return_counts=True)}")
    
    stats = calculate_burn_area_from_arrays(
        pre_nbr,
        post_nbr,
        results['pre']['transform'],
        results['pre']['crs'],
        n_classes,
    )
    print(f"[DEBUG burn_severity] Final stats: {stats}")

    return {
        'severity': severity,
        'dnbr': dnbr,
        'transform': results['pre']['transform'],
        'crs': results['pre']['crs'],
        'stats': stats,
    }
