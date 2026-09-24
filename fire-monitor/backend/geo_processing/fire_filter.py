"""
Fire hotspot filtering to reduce false positives.

Common false positive sources:
- Industrial facilities (flares, power plants)
- Gas flaring (oil/gas infrastructure)
- Solar panel reflections
- Hot roofs / urban heat islands
- Mining / volcanic activity
- Agricultural burning (intentional, not wildfire)
"""

import logging
import time
from django.contrib.gis.geos import Point, Polygon
from django.contrib.gis.db.models.functions import Distance

logger = logging.getLogger(__name__)

# Brightness temperature thresholds (Kelvin)
BRIGHTNESS_THRESHOLDS = {
    'VIIRS_SNPP': {'day': 330, 'night': 290},
    'VIIRS_NOAA20': {'day': 330, 'night': 290},
    'MODIS': {'day': 310, 'night': 290},
}

# Minimum FRP (Fire Radiative Power) in MW
MIN_FRP = {
    'VIIRS_SNPP': 3.0,
    'VIIRS_NOAA20': 3.0,
    'MODIS': 5.0,
}

# Buffer distance for industrial zone check (meters)
INDUSTRIAL_ZONE_BUFFER_M = 500

# Cache for industrial zones (TTL 15 minutes)
_industrial_zones_cache = {
    'zones': None,
    'timestamp': 0,
}
CACHE_TTL_SECONDS = 900  # 15 minutes


def _get_industrial_zones_cached():
    """
    Get industrial zones with caching.
    Returns list of (zone_type, name, geometry) tuples.
    Cache is invalidated after CACHE_TTL_SECONDS or when update_industrial_zones is called.
    """
    from fires.models import IndustrialZone

    now = time.time()
    if _industrial_zones_cache['zones'] is not None and (now - _industrial_zones_cache['timestamp']) < CACHE_TTL_SECONDS:
        return _industrial_zones_cache['zones']

    # Load zones from database
    zones = list(IndustrialZone.objects.values_list('zone_type', 'name', 'geometry'))
    _industrial_zones_cache['zones'] = zones
    _industrial_zones_cache['timestamp'] = now
    logger.info(f'Loaded {len(zones)} industrial zones into cache')
    return zones


def invalidate_industrial_zones_cache():
    """Invalidate the industrial zones cache. Call this after update_industrial_zones."""
    _industrial_zones_cache['zones'] = None
    _industrial_zones_cache['timestamp'] = 0
    logger.info('Industrial zones cache invalidated')


def is_in_industrial_zone(lat, lon, buffer_m=INDUSTRIAL_ZONE_BUFFER_M):
    """
    Check if a point is within or near an industrial zone.

    Uses cached zones with prepared geometries for fast spatial checks.

    Args:
        lat: Latitude
        lon: Longitude
        buffer_m: Buffer distance in meters (default: 500m)

    Returns:
        Tuple of (is_industrial, zone_type, zone_name)
    """
    try:
        point = Point(lon, lat, srid=4326)
        buffer_deg = buffer_m / 111000  # Approximate degrees

        # Get cached zones
        zones = _get_industrial_zones_cached()

        for zone_type, zone_name, geometry in zones:
            if geometry is None:
                continue
            # Quick bbox check first
            if not geometry.envelope.contains(point):
                # Check if point is within buffer distance of bbox
                bbox = geometry.envelope
                if bbox.xmin - buffer_deg <= lon <= bbox.xmax + buffer_deg and \
                   bbox.ymin - buffer_deg <= lat <= bbox.ymax + buffer_deg:
                    # Full distance check
                    if geometry.dwithin(point, buffer_deg):
                        return True, zone_type, zone_name
            else:
                # Point is within bbox, check actual geometry
                if geometry.dwithin(point, buffer_deg):
                    return True, zone_type, zone_name

        return False, None, None

    except Exception as e:
        logger.warning(f'Industrial zone check failed: {e}')
        return False, None, None


def filter_fire_hotspot(fire, apply_all=True):
    """
    Evaluate a fire hotspot and determine if it's likely a false positive.

    Args:
        fire: Dict with keys: brightness, brightness_31, confidence,
              frp, source, daynight, latitude, longitude, type
        apply_all: Apply all filters (if False, only basic temperature filter)

    Returns:
        Tuple of (is_valid, reason) where:
        - is_valid: True if the fire is likely real
        - reason: Explanation if filtered out, empty string if valid
    """
    source = fire.get('source', '')
    daynight = fire.get('daynight', 'D')
    brightness = fire.get('brightness', 0)
    brightness_31 = fire.get('brightness_31')
    frp = fire.get('frp')
    confidence = fire.get('confidence', 'nominal')
    fire_type = fire.get('type', '0')

    # FIRMS type field: 0 = wildfire, 1 = reserved, 2 = oil/gas, 3 = volcanoes
    if fire_type == '2':
        return False, 'oil_gas_flare'
    if fire_type == '3':
        return False, 'volcanic'

    # Basic brightness temperature filter
    thresholds = BRIGHTNESS_THRESHOLDS.get(source, {'day': 310, 'night': 290})
    time_key = 'day' if daynight == 'D' else 'night'
    min_brightness = thresholds[time_key]

    if brightness < min_brightness:
        return False, f'low_brightness ({brightness:.1f}K < {min_brightness}K)'

    # 3.1µm / 3.7µm channel check (better for small fires)
    if brightness_31 is not None and source.startswith('MODIS'):
        if daynight == 'D' and brightness_31 < 350:
            return False, f'low_31um_brightness ({brightness_31:.1f}K)'

    if not apply_all:
        return True, ''

    # FRP filter
    min_frp = MIN_FRP.get(source, 5.0)
    if frp is not None and frp < min_frp and confidence != 'high':
        return False, f'low_frp ({frp:.1f}MW < {min_frp}MW)'

    # Low confidence filter
    if confidence == 'low':
        return False, 'low_confidence'

    # Industrial zone check using spatial join with database
    lat = fire.get('latitude', 0)
    lon = fire.get('longitude', 0)
    is_industrial, zone_type, zone_name = is_in_industrial_zone(lat, lon)

    if is_industrial:
        if frp is not None and frp < 50 and confidence != 'high':
            return False, f'industrial_zone ({zone_type}: {zone_name or "unnamed"})'

    return True, ''


def filter_fire_hotspots(fires, apply_all=True):
    """
    Filter a list of fire hotspots.

    Args:
        fires: List of fire hotspot dicts
        apply_all: Apply all filters

    Returns:
        Tuple of (valid_fires, filtered_fires) where each is a list of dicts.
        Filtered fires include a 'filter_flag' key with the reason.
    """
    valid = []
    filtered = []

    for fire in fires:
        is_valid, reason = filter_fire_hotspot(fire, apply_all)
        if is_valid:
            fire['filter_flag'] = ''
            valid.append(fire)
        else:
            fire['filter_flag'] = reason
            filtered.append(fire)

    logger.info(
        f'Filtered {len(filtered)}/{len(fires)} fire hotspots '
        f'({len(valid)} valid)'
    )
    return valid, filtered


def get_filter_summary(fires, filtered):
    """Get summary of filtering results."""
    reasons = {}
    for fire in filtered:
        reason = fire.get('filter_flag', 'unknown')
        reasons[reason] = reasons.get(reason, 0) + 1

    return {
        'total_input': len(fires) + len(filtered),
        'valid': len(fires),
        'filtered': len(filtered),
        'filter_rate': round(len(filtered) / max(len(fires) + len(filtered), 1) * 100, 1),
        'by_reason': reasons,
    }
