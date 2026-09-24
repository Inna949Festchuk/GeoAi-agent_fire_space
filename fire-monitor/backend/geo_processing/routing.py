"""
Routing and geolocation utilities.
Shared between MCP server and internal chat handler.
"""
import httpx
import logging
import os
from math import radians, sin, cos, sqrt, atan2

logger = logging.getLogger(__name__)

# OSRM API (бесплатный публичный сервис для маршрутов)
OSRM_BASE_URL = "http://router.project-osrm.org"

# Overpass API (OpenStreetMap для поиска объектов)
# Используем несколько серверов для отказоустойчивости
# Порядок важен — ставим рабочие серверы первыми
OVERPASS_SERVERS = [
    "https://overpass.openstreetmap.fr/api/interpreter",  # Французское зеркало (работает стабильно)
    "https://overpass-api.de/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",  # Российское зеркало
]

# Shared httpx client for connection pooling
_http_client = None

# Таймаут одного обращения к Overpass (сек). Раньше использовался общий
# клиент с timeout=30 — при недоступном зеркале перебор 5 серверов мог
# занимать >1 минуту на один вызов. Теперь: короткий таймаут + кэш запросов.
OVERPASS_REQUEST_TIMEOUT = float(os.environ.get('OVERPASS_REQUEST_TIMEOUT', '15'))

# Кэш результатов поиска пожарных частей (per-process, LRU) по параметрам запроса
from collections import OrderedDict
_ROUTE_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_ROUTE_CACHE_MAX = 256
_STATIONS_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_STATIONS_CACHE_MAX = 128


def _get_http_client():
    """Get or create shared httpx client with connection pooling."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(timeout=30.0)
    return _http_client


def _get_overpass_client():
    """Клиент Overpass с укороченным таймаутом (отказоустойчивый перебор зеркал)."""
    global _overpass_client
    if _overpass_client is None:
        _overpass_client = httpx.Client(timeout=OVERPASS_REQUEST_TIMEOUT)
    return _overpass_client

_overpass_client = None


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points using haversine formula (km)."""
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c


def _line_point_at_fraction(coords: list, fraction: float):
    """Возвращает точку на ломаной coords ([[lon,lat],...]) на заданной доле длины."""
    if not coords:
        return None
    if len(coords) == 1:
        return list(coords[0])
    seg_lens = [
        haversine_distance(coords[i][1], coords[i][0], coords[i + 1][1], coords[i][0])
        for i in range(len(coords) - 1)
    ]
    total = sum(seg_lens)
    if total <= 0:
        return list(coords[len(coords) // 2])
    target = total * max(0.0, min(1.0, fraction))
    acc = 0.0
    for i, sl in enumerate(seg_lens):
        if acc + sl >= target:
            t = (target - acc) / sl if sl > 0 else 0.0
            a, b = coords[i], coords[i + 1]
            return [round(a[0] + (b[0] - a[0]) * t, 6), round(a[1] + (b[1] - a[1]) * t, 6)]
        acc += sl
    return list(coords[-1])


def geojson_line_length_km(coords: list) -> float:
    """Длина ломаной GeoJSON в км (по геодезическим расстояниям)."""
    total = 0.0
    for i in range(len(coords) - 1):
        total += haversine_distance(coords[i][1], coords[i][0],
                                    coords[i + 1][1], coords[i + 1][0])
    return total


def add_route_length_labels(geojson: dict) -> dict:
    """
    Для каждого LineString-фичи добавляет точечную фичу-подпись посередине
    линии: label = "N.N км". Идемпотентно (повторный вызов не дублирует).
    Если у линии нет properties.distance_km — длина считается по геометрии.
    """
    features = geojson.get('features', [])
    labels = []
    existing = {f.get('properties', {}).get('label_for')
                for f in features if isinstance(f, dict)}
    for feat_idx, feat in enumerate(features):
        if not isinstance(feat, dict):
            continue
        props = feat.get('properties') or {}
        geom = feat.get('geometry') or {}
        coords = geom.get('coordinates') or []
        if geom.get('type') != 'LineString' or len(coords) < 2:
            continue
        dist = props.get('distance_km')
        if dist is None:
            dist = round(geojson_line_length_km(coords), 1)
        idx = props.get('route_index', feat_idx)
        if f'route-{idx}' in existing:
            continue
        mid = _line_point_at_fraction(geom.get('coordinates') or [], 0.5)
        if mid is None:
            continue
        name = props.get('name') or f'Маршрут {idx + 1}'
        labels.append({
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': mid},
            'properties': {
                'label': f'{dist} км',
                'label_for': f'route-{idx}',
                'name': name,
                'distance_km': dist,
                'duration_min': props.get('duration_min'),
                'type': 'route_label',
            },
        })
    if labels:
        geojson['features'] = features + labels
    return geojson


def build_route_cached(start: list, end: list) -> dict:
    """
    build_route с in-memory LRU-кэшем по координатам (округление ~10 м).
    Повторные запросы (например, подписывание длин уже построенных маршрутов)
    выполняются мгновенно, без повторных обращений к OSRM.
    """
    key = (round(float(start[0]), 4), round(float(start[1]), 4),
           round(float(end[0]), 4), round(float(end[1]), 4))
    cached = _ROUTE_CACHE.get(key)
    if cached is not None:
        _ROUTE_CACHE.move_to_end(key)
        import copy as _copy
        return {
            'geojson': _copy.deepcopy(cached['geojson']),
            'distance_km': cached['distance_km'],
            'duration_min': cached['duration_min'],
            'success': True,
            'cached': True,
        }

    result = build_route(start, end)
    if result.get('success'):
        import copy as _copy
        _ROUTE_CACHE[key] = {
            'geojson': _copy.deepcopy(result['geojson']),
            'distance_km': result['distance_km'],
            'duration_min': result['duration_min'],
        }
        while len(_ROUTE_CACHE) > _ROUTE_CACHE_MAX:
            _ROUTE_CACHE.popitem(last=False)
    return result


def build_route(start: list, end: list) -> dict:
    """
    Build a driving route between two points using OSRM.
    
    Args:
        start: [longitude, latitude]
        end: [longitude, latitude]
    
    Returns:
        Dict with geojson, distance_km, duration_min, success
    """
    osrm_url = f"{OSRM_BASE_URL}/route/v1/driving/{start[0]},{start[1]};{end[0]},{end[1]}?overview=full&geometries=geojson"

    try:
        client = _get_http_client()
        response = client.get(osrm_url)
        
        if response.status_code != 200:
            return {"error": f"OSRM error: HTTP {response.status_code}", "success": False}
        
        data = response.json()
        
        if data.get('code') != 'Ok':
            return {"error": f"OSRM error: {data.get('message', 'Unknown error')}", "success": False}
        
        route = data['routes'][0]
        
        # Создаём GeoJSON с маршрутом
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": route['geometry'],
                    "properties": {
                        "distance_km": round(route['distance'] / 1000, 2),
                        "duration_min": round(route['duration'] / 60, 1),
                        "type": "route"
                    }
                },
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": start},
                    "properties": {"name": "Start", "type": "start"}
                },
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": end},
                    "properties": {"name": "End", "type": "end"}
                }
            ]
        }
        
        return {
            "geojson": geojson,
            "distance_km": round(route['distance'] / 1000, 2),
            "duration_min": round(route['duration'] / 60, 1),
            "success": True
        }
    
    except Exception as e:
        logger.error(f"Failed to build route: {e}")
        return {"error": f"Failed to build route: {str(e)}", "success": False}


def build_routes_batch(route_pairs: list) -> dict:
    """
    Build multiple driving routes at once.

    Args:
        route_pairs: List of [start, end] pairs, where each is [lon, lat]
                    Example: [[[37.6, 55.7], [37.9, 55.8]], [[37.5, 55.6], [37.8, 55.9]]]

    Returns:
        Dict with combined geojson (all routes + points), total stats, success
    """
    all_features = []
    total_distance = 0
    total_duration = 0
    success_count = 0
    errors = []

    for i, pair in enumerate(route_pairs):
        if len(pair) != 2:
            errors.append(f"Route {i}: invalid pair format")
            continue

        start, end = pair
        result = build_route_cached(start, end)

        if result.get('success'):
            success_count += 1
            total_distance += result['distance_km']
            total_duration += result['duration_min']

            # Добавляем маршрут с метаданными
            route_geojson = result['geojson']
            for feature in route_geojson['features']:
                if feature['properties'].get('type') == 'route':
                    feature['properties']['route_index'] = i
                    feature['properties']['name'] = f"Route {i+1}"
                elif feature['properties'].get('type') == 'start':
                    feature['properties']['name'] = f"Start {i+1}"
                    feature['properties']['route_index'] = i
                elif feature['properties'].get('type') == 'end':
                    feature['properties']['name'] = f"End {i+1}"
                    feature['properties']['route_index'] = i
                all_features.append(feature)
        else:
            errors.append(f"Route {i}: {result.get('error', 'Unknown error')}")

    combined_geojson = {
        "type": "FeatureCollection",
        "features": all_features
    }

    return {
        "geojson": combined_geojson,
        "total_routes": len(route_pairs),
        "successful_routes": success_count,
        "total_distance_km": round(total_distance, 2),
        "total_duration_min": round(total_duration, 1),
        "errors": errors if errors else None,
        "success": success_count > 0
    }


def find_nearest_fire_stations(lat: float = None, lon: float = None, radius_km: int = 50, limit: int = None, bbox: list = None) -> dict:
    """
    Find fire stations using Overpass API (OpenStreetMap).
    
    Can search in two modes:
    1. Around a point (lat, lon, radius_km) - returns nearest stations
    2. In a bounding box (bbox) - returns all stations in area
    
    Args:
        lat: Latitude of center point (optional if bbox provided)
        lon: Longitude of center point (optional if bbox provided)
        radius_km: Search radius in kilometers (default: 50, used only with lat/lon)
        limit: Maximum number of stations to return (default: None = return all)
        bbox: Bounding box [min_lon, min_lat, max_lon, max_lat] (optional)
    
    Returns:
        Dict with geojson, stations list, total_found, success
    """
    # Кэш по параметрам запроса: повторные обращения (перестроение маршрутов,
    # подписывание длин) не идут в Overpass вообще.
    cache_key = (
        None if lat is None else round(float(lat), 3),
        None if lon is None else round(float(lon), 3),
        radius_km, limit,
        None if not bbox else tuple(round(float(v), 3) for v in bbox),
    )
    cached = _STATIONS_CACHE.get(cache_key)
    if cached is not None:
        _STATIONS_CACHE.move_to_end(cache_key)
        import copy as _copy
        result = _copy.deepcopy(cached)
        result['cached'] = True
        return result

    # Определяем режим поиска
    if bbox and len(bbox) == 4:
        # Поиск в bounding box
        min_lon, min_lat, max_lon, max_lat = bbox
        query = f"""
        [out:json][timeout:25];
        (
          node["amenity"="fire_station"]({min_lat},{min_lon},{max_lat},{max_lon});
          way["amenity"="fire_station"]({min_lat},{min_lon},{max_lat},{max_lon});
          relation["amenity"="fire_station"]({min_lat},{min_lon},{max_lat},{max_lon});
          node["emergency"="fire_station"]({min_lat},{min_lon},{max_lat},{max_lon});
          way["emergency"="fire_station"]({min_lat},{min_lon},{max_lat},{max_lon});
          way["building"="fire_station"]({min_lat},{min_lon},{max_lat},{max_lon});
          node["office"="government"]["government"="fire_service"]({min_lat},{min_lon},{max_lat},{max_lon});
        );
        out center;
        """
        search_description = f"bbox [{min_lon}, {min_lat}, {max_lon}, {max_lat}]"
        center_lat = (min_lat + max_lat) / 2
        center_lon = (min_lon + max_lon) / 2
    elif lat is not None and lon is not None:
        # Поиск вокруг точки
        radius_m = radius_km * 1000
        query = f"""
        [out:json][timeout:25];
        (
          node["amenity"="fire_station"](around:{radius_m},{lat},{lon});
          way["amenity"="fire_station"](around:{radius_m},{lat},{lon});
          relation["amenity"="fire_station"](around:{radius_m},{lat},{lon});
          node["emergency"="fire_station"](around:{radius_m},{lat},{lon});
          way["emergency"="fire_station"](around:{radius_m},{lat},{lon});
          way["building"="fire_station"](around:{radius_m},{lat},{lon});
          node["office"="government"]["government"="fire_service"](around:{radius_m},{lat},{lon});
        );
        out center;
        """
        search_description = f"radius {radius_km} km from [{lat}, {lon}]"
        center_lat = lat
        center_lon = lon
    else:
        return {"error": "Either bbox or (lat, lon) must be provided", "success": False}

    try:
        # Пробуем серверы по очереди для отказоустойчивости.
        # Отдельный клиент с укороченным таймаутом (OVERPASS_REQUEST_TIMEOUT,
        # по умолчанию 15 с) — чтобы недоступное зеркало не вешало вызов на минуты.
        response = None
        last_error = None
        client = _get_overpass_client()
        for server_url in OVERPASS_SERVERS:
            try:
                logger.info(f"Trying Overpass server: {server_url}")
                response = client.post(
                    server_url,
                    data={'data': query},
                    headers={
                        'User-Agent': 'FireMonitorApp/1.0 (Educational Wildfire Monitoring Project)',
                    }
                )
                if response.status_code == 200:
                    logger.info(f"Success with {server_url}")
                    break
                last_error = f"HTTP {response.status_code} from {server_url}"
                logger.warning(f"Failed with {server_url}: {response.status_code}")
            except Exception as e:
                last_error = f"{str(e)} from {server_url}"
                logger.warning(f"Exception with {server_url}: {e}")
                continue

        if response is None or response.status_code != 200:
            return {"error": f"Overpass API error: {last_error}", "success": False}
        
        data = response.json()
        elements = data.get('elements', [])

        if not elements:
            return {"error": f"No fire stations found in {search_description}", "success": False}

        # Обрабатываем результаты
        stations = []
        features = []

        for el in elements:
            if 'center' in el:
                s_lat, s_lon = el['center']['lat'], el['center']['lon']
            elif 'lat' in el:
                s_lat, s_lon = el['lat'], el['lon']
            else:
                continue

            tags = el.get('tags', {})
            name = tags.get('name', 'Пожарная часть')
            phone = tags.get('phone', '')
            address = tags.get('addr:street', '')

            # Рассчитываем расстояние от центра поиска
            distance_km = haversine_distance(center_lat, center_lon, s_lat, s_lon)

            station = {
                "lat": s_lat,
                "lon": s_lon,
                "name": name,
                "phone": phone,
                "address": address,
                "distance_km": round(distance_km, 2)
            }
            stations.append(station)

            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [s_lon, s_lat]},
                "properties": {
                    "name": name,
                    "phone": phone,
                    "address": address,
                    "distance_km": round(distance_km, 2),
                    "type": "fire_station"
                }
            })

        # Сортируем по расстоянию
        stations.sort(key=lambda x: x['distance_km'])
        features.sort(key=lambda x: x['properties']['distance_km'])

        # Ограничиваем до limit ближайших (если limit указан)
        if limit is not None and limit > 0:
            stations_limited = stations[:limit]
            features_limited = features[:limit]
        else:
            # Возвращаем все найденные
            stations_limited = stations
            features_limited = features

        geojson = {
            "type": "FeatureCollection",
            "features": features_limited
        }

        result = {
            "geojson": geojson,
            "stations": stations_limited,
            "total_found": len(stations),
            "returned_count": len(stations_limited),
            "search_mode": "bbox" if bbox else "radius",
            "success": True
        }
        # кладём в кэш только успешный результат (не пустые ошибки поиска)
        import copy as _copy
        _STATIONS_CACHE[cache_key] = _copy.deepcopy(result)
        while len(_STATIONS_CACHE) > _STATIONS_CACHE_MAX:
            _STATIONS_CACHE.popitem(last=False)
        return result
    
    except Exception as e:
        logger.error(f"Failed to find fire stations: {e}")
        return {"error": f"Failed to find fire stations: {str(e)}", "success": False}
