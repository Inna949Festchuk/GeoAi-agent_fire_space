import { useEffect, useRef, useMemo } from 'react'
import maplibregl from 'maplibre-gl'

function MapView({ 
  fireData, burnData, customData, 
  showFires, showBurns, showRoutes, showFireStations, showCustom,
  onMapMove 
}) {
  const mapRef = useRef(null)
  const mapContainerRef = useRef(null)
  const moveTimeoutRef = useRef(null)

  // Разделяем customData на категории
  const { routesData, fireStationsData, otherCustomData } = useMemo(() => {
    if (!customData?.features) {
      return { routesData: null, fireStationsData: null, otherCustomData: null }
    }

    const routes = customData.features.filter(f => 
      f.geometry?.type === 'LineString' || f.geometry?.type === 'MultiLineString'
    )
    const stations = customData.features.filter(f => 
      f.properties?.type === 'fire_station'
    )
    const other = customData.features.filter(f => 
      f.geometry?.type !== 'LineString' && 
      f.geometry?.type !== 'MultiLineString' && 
      f.properties?.type !== 'fire_station'
    )

    return {
      routesData: routes.length > 0 ? { type: 'FeatureCollection', features: routes } : null,
      fireStationsData: stations.length > 0 ? { type: 'FeatureCollection', features: stations } : null,
      otherCustomData: other.length > 0 ? { type: 'FeatureCollection', features: other } : null,
    }
  }, [customData])

  useEffect(() => {
    if (mapRef.current) return

    const map = new maplibregl.Map({
      container: mapContainerRef.current,
      style: {
        version: 8,
        sources: {
          'osm-tiles': {
            type: 'raster',
            tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
            tileSize: 256,
            attribution: '© OpenStreetMap contributors',
          },
        },
        layers: [
          {
            id: 'osm-tiles',
            type: 'raster',
            source: 'osm-tiles',
            minzoom: 0,
            maxzoom: 19,
          },
        ],
      },
      center: [95, 62], // Center on Siberia
      zoom: 4,
    })

    map.addControl(new maplibregl.NavigationControl(), 'top-left')
    map.addControl(new maplibregl.ScaleControl(), 'bottom-left')

    map.on('moveend', () => {
      if (moveTimeoutRef.current) clearTimeout(moveTimeoutRef.current)
      moveTimeoutRef.current = setTimeout(() => {
        const bounds = map.getBounds()
        const bbox = [
          bounds.getWest(),
          bounds.getSouth(),
          bounds.getEast(),
          bounds.getNorth(),
        ]
        onMapMove?.(bbox)
      }, 500)
    })

    mapRef.current = map

    return () => {
      map.remove()
      mapRef.current = null
    }
  }, [])

  // Update fire layer
  useEffect(() => {
    const map = mapRef.current
    if (!map) return

    if (map.getLayer('fire-points')) map.removeLayer('fire-points')
    if (map.getSource('fires')) map.removeSource('fires')

    if (showFires && fireData?.features?.length > 0) {
      map.addSource('fires', {
        type: 'geojson',
        data: fireData,
      })

      map.addLayer({
        id: 'fire-points',
        type: 'circle',
        source: 'fires',
        paint: {
          'circle-radius': [
            'interpolate', ['linear'],
            ['get', 'frp'],
            0, 6,
            50, 12,
            200, 20,
          ],
          'circle-color': [
            'match',
            ['get', 'confidence'],
            'high', '#e94560',
            'nominal', '#ff8c42',
            'low', '#4ade80',
            '#ff6b6b',
          ],
          'circle-opacity': 0.85,
          'circle-stroke-width': 2,
          'circle-stroke-color': '#fff',
        },
      })

      // Popup on click
      map.on('click', 'fire-points', (e) => {
        const props = e.features[0].properties
        const coords = e.features[0].geometry.coordinates

        new maplibregl.Popup({ className: 'map-popup-container', closeButton: false })
          .setLngLat(coords)
          .setHTML(`
            <div class="map-popup">
              <strong>Очаг пожара</strong>
              <div class="popup-row"><span class="popup-label">Источник:</span> <span class="popup-value">${props.source}</span></div>
              <div class="popup-row"><span class="popup-label">Яркость:</span> <span class="popup-value">${Number(props.brightness).toFixed(1)} K</span></div>
              <div class="popup-row"><span class="popup-label">Уверенность:</span> <span class="popup-value">${props.confidence}</span></div>
              <div class="popup-row"><span class="popup-label">FRP:</span> <span class="popup-value">${props.frp ? Number(props.frp).toFixed(1) + ' MW' : 'N/A'}</span></div>
              <div class="popup-row"><span class="popup-label">Время:</span> <span class="popup-value">${new Date(props.detected_at).toLocaleString('ru-RU')}</span></div>
            </div>
          `)
          .addTo(map)
      })

      map.on('mouseenter', 'fire-points', () => {
        map.getCanvas().style.cursor = 'pointer'
      })
      map.on('mouseleave', 'fire-points', () => {
        map.getCanvas().style.cursor = ''
      })
    }
  }, [fireData, showFires])

  // Update burn layer
  useEffect(() => {
    const map = mapRef.current
    if (!map) return

    if (map.getLayer('burn-polygons')) map.removeLayer('burn-polygons')
    if (map.getLayer('burn-outlines')) map.removeLayer('burn-outlines')
    if (map.getSource('burns')) map.removeSource('burns')

    if (showBurns && burnData?.features?.length > 0) {
      map.addSource('burns', {
        type: 'geojson',
        data: burnData,
      })

      // Burn severity color scheme
      const severityColors = [
        'match',
        ['get', 'severity'],
        'high', '#dc2626',      // Red for high severity
        'moderate', '#ea580c',  // Orange for moderate severity
        'low', '#ca8a04',       // Yellow for low severity
        'unburned', '#65a30d',  // Green for unburned
        '#6b7280',              // Gray fallback
      ]

      map.addLayer({
        id: 'burn-polygons',
        type: 'fill',
        source: 'burns',
        paint: {
          'fill-color': severityColors,
          'fill-opacity': 0.6,
        },
      })

      map.addLayer({
        id: 'burn-outlines',
        type: 'line',
        source: 'burns',
        paint: {
          'line-color': severityColors,
          'line-width': 2,
          'line-opacity': 0.8,
        },
      })

      // Auto-center on first burn polygon
      const firstFeature = burnData.features[0]
      if (firstFeature && firstFeature.geometry) {
        // Calculate bounds from first feature
        const coords = firstFeature.geometry.coordinates
        let bounds = new maplibregl.LngLatBounds()
        
        // Helper function to extract coordinates recursively
        const extractCoords = (coord) => {
          if (typeof coord[0] === 'number') {
            // It's a point [lng, lat]
            bounds.extend(coord)
          } else {
            // It's an array of coordinates
            coord.forEach(extractCoords)
          }
        }
        
        extractCoords(coords)
        
        // Smoothly fly to the bounds
        map.fitBounds(bounds, {
          padding: 50,
          maxZoom: 12,
          duration: 1500, // 1.5 seconds animation
          easing: (t) => t * (2 - t) // ease-out
        })
      }

      // Popup on click
      map.on('click', 'burn-polygons', (e) => {
        const props = e.features[0].properties
        const coords = e.lngLat

        const severityLabels = {
          'high': 'Высокая степень',
          'moderate': 'Средняя степень',
          'low': 'Низкая степень',
          'unburned': 'Не затронуто',
        }

        new maplibregl.Popup({ className: 'map-popup-container', closeButton: false })
          .setLngLat(coords)
          .setHTML(`
            <div class="map-popup">
              <strong>Горевшая территория</strong>
              <div class="popup-row"><span class="popup-label">Степень:</span> <span class="popup-value">${severityLabels[props.severity] || props.severity}</span></div>
              <div class="popup-row"><span class="popup-label">Площадь:</span> <span class="popup-value">${Number(props.area_ha).toFixed(2)} га</span></div>
              <div class="popup-row"><span class="popup-label">Средний dNBR:</span> <span class="popup-value">${Number(props.mean_dnbr).toFixed(1)}</span></div>
              <div class="popup-row"><span class="popup-label">Период:</span> <span class="popup-value">${props.pre_date} — ${props.post_date}</span></div>
            </div>
          `)
          .addTo(map)
      })

      map.on('mouseenter', 'burn-polygons', () => {
        map.getCanvas().style.cursor = 'pointer'
      })
      map.on('mouseleave', 'burn-polygons', () => {
        map.getCanvas().style.cursor = ''
      })
    }
  }, [burnData, showBurns])

  // Update routes layer
  useEffect(() => {
    const map = mapRef.current
    if (!map) return

    // ОЧИСТКА
    if (map.getLayer('routes-line-layer')) map.removeLayer('routes-line-layer')
    if (map.getLayer('routes-points-layer')) map.removeLayer('routes-points-layer')
    if (map.getSource('routes-line-data')) map.removeSource('routes-line-data')
    if (map.getSource('routes-points-data')) map.removeSource('routes-points-data')

    if (!showRoutes || !routesData?.features?.length) return

    // Линии маршрутов
    map.addSource('routes-line-data', { type: 'geojson', data: routesData })
    map.addLayer({
      id: 'routes-line-layer',
      type: 'line',
      source: 'routes-line-data',
      paint: {
        'line-color': '#ff6b35',
        'line-width': 4,
        'line-dasharray': [2, 2],
      },
    })

    // Точки старта/финиша
    const routePoints = routesData.features.filter(f => 
      f.properties?.type === 'start' || f.properties?.type === 'end'
    )
    if (routePoints.length > 0) {
      map.addSource('routes-points-data', { 
        type: 'geojson', 
        data: { type: 'FeatureCollection', features: routePoints }
      })
      map.addLayer({
        id: 'routes-points-layer',
        type: 'circle',
        source: 'routes-points-data',
        paint: {
          'circle-radius': 10,
          'circle-color': ['match', ['get', 'type'], 'start', '#4ade80', '#e94560'],
          'circle-stroke-width': 2,
          'circle-stroke-color': '#fff',
        },
      })
    }

    // Попап для маршрутов
    map.on('click', 'routes-line-layer', (e) => {
      const props = e.features[0].properties
      let content = '<strong>🛣️ Маршрут</strong>'
      if (props.distance_km) content += `<div class="popup-row"><span class="popup-label">Расстояние:</span> <span class="popup-value">${props.distance_km} км</span></div>`
      if (props.duration_min) content += `<div class="popup-row"><span class="popup-label">Время:</span> <span class="popup-value">${props.duration_min} мин</span></div>`
      new maplibregl.Popup({ className: 'map-popup-container', closeButton: false })
        .setLngLat(e.lngLat)
        .setHTML(`<div class="map-popup">${content}</div>`)
        .addTo(map)
    })
  }, [routesData, showRoutes])

  // Update fire stations layer
  useEffect(() => {
    const map = mapRef.current
    if (!map) return

    // ОЧИСТКА
    if (map.getLayer('fire-stations-layer')) map.removeLayer('fire-stations-layer')
    if (map.getSource('fire-stations-data')) map.removeSource('fire-stations-data')

    if (!showFireStations || !fireStationsData?.features?.length) return

    map.addSource('fire-stations-data', { type: 'geojson', data: fireStationsData })
    map.addLayer({
      id: 'fire-stations-layer',
      type: 'circle',
      source: 'fire-stations-data',
      paint: {
        'circle-radius': 8,
        'circle-color': '#00d4ff',
        'circle-stroke-width': 2,
        'circle-stroke-color': '#fff',
      },
    })

    map.on('click', 'fire-stations-layer', (e) => {
      const props = e.features[0].properties
      const coords = e.features[0].geometry.coordinates
      let content = `<strong>🚒 ${props.name || 'Пожарная часть'}</strong>`
      if (props.address) content += `<div class="popup-row"><span class="popup-label">Адрес:</span> <span class="popup-value">${props.address}</span></div>`
      if (props.phone) content += `<div class="popup-row"><span class="popup-label">Телефон:</span> <span class="popup-value">${props.phone}</span></div>`
      if (props.distance_km) content += `<div class="popup-row"><span class="popup-label">Расстояние:</span> <span class="popup-value">${props.distance_km} км</span></div>`
      new maplibregl.Popup({ className: 'map-popup-container', closeButton: false })
        .setLngLat(coords)
        .setHTML(`<div class="map-popup">${content}</div>`)
        .addTo(map)
    })

    map.on('mouseenter', 'fire-stations-layer', () => { map.getCanvas().style.cursor = 'pointer' })
    map.on('mouseleave', 'fire-stations-layer', () => { map.getCanvas().style.cursor = '' })
  }, [fireStationsData, showFireStations])

  // Update other custom layer (sandbox results)
  useEffect(() => {
    const map = mapRef.current
    if (!map) return

    // ОЧИСТКА
    if (map.getLayer('custom-polygon-layer')) map.removeLayer('custom-polygon-layer')
    if (map.getLayer('custom-polygon-outline')) map.removeLayer('custom-polygon-outline')
    if (map.getLayer('custom-point-labels')) map.removeLayer('custom-point-labels')
    if (map.getLayer('custom-point-layer')) map.removeLayer('custom-point-layer')
    if (map.getSource('custom-polygon-data')) map.removeSource('custom-polygon-data')
    if (map.getSource('custom-point-data')) map.removeSource('custom-point-data')

    if (!showCustom || !otherCustomData?.features?.length) return

    const points = otherCustomData.features.filter(f => 
      f.geometry?.type === 'Point' || f.geometry?.type === 'MultiPoint'
    )
    const polygons = otherCustomData.features.filter(f => 
      f.geometry?.type === 'Polygon' || f.geometry?.type === 'MultiPolygon'
    )

    if (polygons.length > 0) {
      const polygonCollection = { type: 'FeatureCollection', features: polygons }
      map.addSource('custom-polygon-data', { type: 'geojson', data: polygonCollection })
      map.addLayer({
        id: 'custom-polygon-layer',
        type: 'fill',
        source: 'custom-polygon-data',
        paint: { 'fill-color': '#9d4edd', 'fill-opacity': 0.5 },
      })
      map.addLayer({
        id: 'custom-polygon-outline',
        type: 'line',
        source: 'custom-polygon-data',
        paint: { 'line-color': '#9d4edd', 'line-width': 2, 'line-opacity': 0.8 },
      })
    }

    if (points.length > 0) {
      const pointCollection = { type: 'FeatureCollection', features: points }
      map.addSource('custom-point-data', { type: 'geojson', data: pointCollection })
      map.addLayer({
        id: 'custom-point-layer',
        type: 'circle',
        source: 'custom-point-data',
        paint: {
          'circle-radius': 8,
          'circle-color': '#9d4edd',
          'circle-stroke-width': 2,
          'circle-stroke-color': '#fff',
        },
      })

      // Добавляем текстовые подписи для точек с properties.label
      const hasLabels = points.some(p => p.properties?.label)
      if (hasLabels) {
        map.addLayer({
          id: 'custom-point-labels',
          type: 'symbol',
          source: 'custom-point-data',
          filter: ['has', 'label'],
          layout: {
            'text-field': ['get', 'label'],
            'text-size': 12,
            'text-offset': [0, 1.5],
            'text-anchor': 'top',
            'text-allow-overlap': true,
          },
          paint: {
            'text-color': '#fff',
            'text-halo-color': '#000',
            'text-halo-width': 1.5,
          },
        })
      }
    }
  }, [otherCustomData, showCustom])

  return <div ref={mapContainerRef} style={{ width: '100%', height: '100%' }} />
}

export default MapView
