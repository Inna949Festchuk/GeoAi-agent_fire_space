"""
Routing and geolocation utilities.
Shared between MCP server and internal chat handler.
"""
import httpx
import logging
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


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points using haversine formula (km)."""
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c


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
        with httpx.Client(timeout=30.0) as client:
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
        result = build_route(start, end)

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


def find_nearest_fire_stations(lat: float, lon: float, radius_km: int = 50, limit: int = 5) -> dict:
    """
    Find nearest fire stations using Overpass API (OpenStreetMap).

    Args:
        lat: Latitude
        lon: Longitude
        radius_km: Search radius in kilometers
        limit: Maximum number of stations to return (default: 5)

    Returns:
        Dict with geojson, stations list, total_found, success
    """
    radius_m = radius_km * 1000

    # Расширенный поиск по нескольким тегам OSM
    # В России пожарные части обычно имеют тег amenity=fire_station
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

    try:
        # Пробуем серверы по очереди для отказоустойчивости
        response = None
        last_error = None
        for server_url in OVERPASS_SERVERS:
            try:
                logger.info(f"Trying Overpass server: {server_url}")
                with httpx.Client(timeout=35.0) as client:
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
            return {"error": f"No fire stations found within {radius_km} km", "success": False}
        
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
            
            distance_km = haversine_distance(lat, lon, s_lat, s_lon)
            
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

        # Ограничиваем до limit ближайших
        stations_limited = stations[:limit]
        features_limited = features[:limit]

        geojson = {
            "type": "FeatureCollection",
            "features": features_limited
        }

        return {
            "geojson": geojson,
            "stations": stations_limited,
            "total_found": len(stations),
            "success": True
        }
    
    except Exception as e:
        logger.error(f"Failed to find fire stations: {e}")
        return {"error": f"Failed to find fire stations: {str(e)}", "success": False}
