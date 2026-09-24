"""
NASA FIRMS (Fire Information for Resource Management System) API client.

Downloads active fire hotspots from MODIS and VIIRS sensors.
API docs: https://firms.modaps.eosdis.nasa.gov/api/
"""

import requests
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from django.conf import settings
from django.contrib.gis.geos import Point

logger = logging.getLogger(__name__)

FIRMS_API_BASE = 'https://firms.modaps.eosdis.nasa.gov/api'

# Source mapping for FIRMS API
SOURCE_MAP = {
    'VIIRS_SNPP': 'VIIRS_SNPP_NRT',
    'VIIRS_NOAA20': 'VIIRS_NOAA20_NRT',
    'MODIS_Aqua': 'MODIS',
    'MODIS_Terra': 'MODIS',
}

# Shared requests session for connection pooling
_session = None


def _get_session():
    """Get or create shared requests session with connection pooling."""
    global _session
    if _session is None:
        _session = requests.Session()
        # Configure connection pool
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
            max_retries=3
        )
        _session.mount('https://', adapter)
        _session.mount('http://', adapter)
    return _session


def get_firms_api_key():
    return getattr(settings, 'FIRMS_API_KEY', '')


def fetch_active_fires(
    source='VIIRS_SNPP',
    bbox=None,
    days=1,
    api_key=None,
):
    """
    Fetch active fire data from NASA FIRMS.

    Args:
        source: Sensor source (VIIRS_SNPP, VIIRS_NOAA20, MODIS_Aqua, MODIS_Terra)
        bbox: Bounding box as (minx, miny, maxx, maxy) in WGS84
        days: Number of days to look back (1-5 for NRT, 1-10 for archive)
        api_key: FIRMS API key (defaults to settings.FIRMS_API_KEY)

    Returns:
        List of fire hotspot dicts with keys:
        latitude, longitude, brightness, confidence, frp,
        detected_at, source, satellite, instrument, daynight
    """
    api_key = api_key or get_firms_api_key()
    if not api_key:
        logger.warning('FIRMS API key not configured')
        return []

    api_source = SOURCE_MAP.get(source, source)
    area = 'world'

    if bbox:
        minx, miny, maxx, maxy = bbox
        # FIRMS API expects: west,south,east,north (lon,lat,lon,lat)
        area = f'{minx},{miny},{maxx},{maxy}'

    # NRT sources only support 1-5 days, archive supports 1-10 days
    # For simplicity, cap at 5 days for all sources
    days = min(days, 5)

    # Use /api/area/csv/ endpoint for bounding box queries
    url = f'{FIRMS_API_BASE}/area/csv/{api_key}/{api_source}/{area}/{days}'

    try:
        session = _get_session()
        response = session.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error(f'FIRMS API request failed: {e}')
        return []

    # FIRMS /area/csv/ endpoint returns CSV format
    import csv
    import io
    
    try:
        reader = csv.DictReader(io.StringIO(response.text))
        data = list(reader)
    except Exception as e:
        logger.error(f'Failed to parse FIRMS CSV response: {e}')
        return []

    if not data:
        return []

    fires = []
    for item in data:
        try:
            fire = parse_firms_record(item, source)
            if fire:
                fires.append(fire)
        except Exception as e:
            logger.warning(f'Failed to parse FIRMS record: {e}')
            continue

    logger.info(f'Fetched {len(fires)} fire hotspots from FIRMS ({source})')
    return fires


def parse_firms_record(record, source):
    """Parse a single FIRMS CSV record into a standardized dict."""
    try:
        lat = float(record.get('latitude', 0))
        lon = float(record.get('longitude', 0))

        if lat == 0 and lon == 0:
            return None

        # VIIRS uses bright_ti4/bright_ti5, MODIS uses brightness/bright_t31
        brightness = 0.0
        if 'brightness' in record and record['brightness']:
            try:
                brightness = float(record['brightness'])
            except (ValueError, TypeError):
                pass
        elif 'bright_ti4' in record and record['bright_ti4']:
            try:
                brightness = float(record['bright_ti4'])
            except (ValueError, TypeError):
                pass
        
        # 3.1µm / 3.7µm channel
        brightness_31 = None
        if 'bright_t31' in record and record['bright_t31']:
            try:
                brightness_31 = float(record['bright_t31'])
            except (ValueError, TypeError):
                pass
        elif 'bright_ti5' in record and record['bright_ti5']:
            try:
                brightness_31 = float(record['bright_ti5'])
            except (ValueError, TypeError):
                pass

        # FRP (Fire Radiative Power)
        frp = None
        if 'frp' in record and record['frp']:
            try:
                frp = float(record['frp'])
            except (ValueError, TypeError):
                pass

        # Confidence: VIIRS uses 'low/nominal/high', MODIS uses 'l/n/h' or numeric
        confidence_raw = record.get('confidence', '')
        if isinstance(confidence_raw, str):
            confidence_map = {'l': 'low', 'n': 'nominal', 'h': 'high',
                            'low': 'low', 'nominal': 'nominal', 'high': 'high'}
            confidence = confidence_map.get(confidence_raw.lower(), 'nominal')
        elif isinstance(confidence_raw, (int, float)):
            if confidence_raw >= 80:
                confidence = 'high'
            elif confidence_raw >= 40:
                confidence = 'nominal'
            else:
                confidence = 'low'
        else:
            confidence = 'nominal'

        # Parse date and time (FIRMS times are in UTC)
        date_str = record.get('acq_date', '')
        time_str = record.get('acq_time', '0000')
        if date_str:
            try:
                # Ensure time_str is 4 digits
                time_str = str(time_str).zfill(4)
                time_formatted = f'{time_str[:2]}:{time_str[2:]}'
                dt = datetime.strptime(f'{date_str} {time_formatted}', '%Y-%m-%d %H:%M')
                dt = dt.replace(tzinfo=ZoneInfo('UTC'))
            except ValueError:
                try:
                    dt = datetime.strptime(date_str, '%Y-%m-%d')
                    dt = dt.replace(tzinfo=ZoneInfo('UTC'))
                except ValueError:
                    dt = datetime.now(ZoneInfo('UTC'))
        else:
            dt = datetime.now(ZoneInfo('UTC'))

        return {
            'latitude': lat,
            'longitude': lon,
            'brightness': brightness,
            'brightness_31': brightness_31,
            'confidence': confidence,
            'frp': frp,
            'source': source,
            'satellite': record.get('satellite', ''),
            'instrument': record.get('instrument', ''),
            'detected_at': dt,
            'daynight': record.get('daynight', 'D'),
            'type': str(record.get('type', '0')),
        }
    except (ValueError, TypeError) as e:
        logger.warning(f'Error parsing FIRMS record: {e}')
        return None


def fetch_all_sources(bbox=None, days=1, api_key=None):
    """
    Fetch active fires from all available sources in parallel.
    Uses ThreadPoolExecutor for ~4x speedup when fetching from 4 sources.
    """
    all_fires = []

    # Fetch from all sources in parallel
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_active_fires, source=source, bbox=bbox, days=days, api_key=api_key): source
            for source in SOURCE_MAP
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                fires = future.result()
                all_fires.extend(fires)
                logger.debug(f'Fetched {len(fires)} fires from {source}')
            except Exception as e:
                logger.error(f'Error fetching from {source}: {e}')

    logger.info(f'Total fires fetched from all sources: {len(all_fires)}')
    return all_fires
