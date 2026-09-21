import { useEffect, useRef } from 'react'
import maplibregl from 'maplibre-gl'

function MapView({ fireData, burnData, customData, showFires, showBurns, onMapMove }) {
  const mapRef = useRef(null)
  const mapContainerRef = useRef(null)
  const moveTimeoutRef = useRef(null)

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

    // Add legend
    const legend = document.createElement('div')
    legend.className = 'map-legend'
    legend.innerHTML = `
      <div class="legend-title">Уровень уверенности</div>
      <div class="legend-item">
        <span class="legend-dot" style="background: #e94560;"></span>
        <span class="legend-text">Высокая — реальный пожар</span>
      </div>
      <div class="legend-item">
        <span class="legend-dot" style="background: #ff8c42;"></span>
        <span class="legend-text">Номинальная — вероятность средняя</span>
      </div>
      <div class="legend-item">
        <span class="legend-dot" style="background: #4ade80;"></span>
        <span class="legend-text">Низкая — возможно ложное срабатывание</span>
      </div>
    `
    map.getContainer().appendChild(legend)

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

  // Update custom layer (from sandbox execute_python)
  useEffect(() => {
    const map = mapRef.current
    if (!map) return

    // ОЧИСТКА: Удаляем старые custom-слои
    const layerIds = ['custom-line-layer', 'custom-point-layer', 'custom-polygon-layer', 'custom-polygon-outline']
    const sourceIds = ['custom-line-data', 'custom-point-data', 'custom-polygon-data']

    layerIds.forEach(layerId => {
      if (map.getLayer(layerId)) map.removeLayer(layerId)
    })
    sourceIds.forEach(sourceId => {
      if (map.getSource(sourceId)) map.removeSource(sourceId)
    })

    if (!customData) return

    // Нормализуем данные: если это один Feature, оборачиваем в FeatureCollection
    let geojson = customData
    if (customData.type === 'Feature') {
      geojson = { type: 'FeatureCollection', features: [customData] }
    } else if (customData.type !== 'FeatureCollection') {
      return
    }

    if (geojson.features?.length > 0) {
      // Разделяем features по типу геометрии (поддержка смешанных коллекций)
      const points = geojson.features.filter(f =>
        f.geometry?.type === 'Point' || f.geometry?.type === 'MultiPoint'
      )
      const lines = geojson.features.filter(f =>
        f.geometry?.type === 'LineString' || f.geometry?.type === 'MultiLineString'
      )
      const polygons = geojson.features.filter(f =>
        f.geometry?.type === 'Polygon' || f.geometry?.type === 'MultiPolygon'
      )

      // Отображаем линии (маршруты)
      if (lines.length > 0) {
        const lineCollection = { type: 'FeatureCollection', features: lines }
        map.addSource('custom-line-data', { type: 'geojson', data: lineCollection })
        map.addLayer({
          id: 'custom-line-layer',
          type: 'line',
          source: 'custom-line-data',
          paint: {
            'line-color': '#ff6b35',
            'line-width': 4,
            'line-dasharray': [2, 2],
          },
        })
        
        // Попап для линий маршрутов
        map.on('click', 'custom-line-layer', (e) => {
          const props = e.features[0].properties
          const coordinates = e.lngLat
          
          let content = '<strong>🛣️ Маршрут</strong>'
          if (props.distance_km) {
            content += `<div class="popup-row"><span class="popup-label">Расстояние:</span> <span class="popup-value">${props.distance_km} км</span></div>`
          }
          if (props.duration_min) {
            content += `<div class="popup-row"><span class="popup-label">Время:</span> <span class="popup-value">${props.duration_min} мин</span></div>`
          }
          if (props.name) {
            content += `<div class="popup-row"><span class="popup-label">Название:</span> <span class="popup-value">${props.name}</span></div>`
          }
          
          new maplibregl.Popup({ className: 'map-popup-container', closeButton: false })
            .setLngLat(coordinates)
            .setHTML(`<div class="map-popup">${content}</div>`)
            .addTo(map)
        })
        
        map.on('mouseenter', 'custom-line-layer', () => {
          map.getCanvas().style.cursor = 'pointer'
        })
        map.on('mouseleave', 'custom-line-layer', () => {
          map.getCanvas().style.cursor = ''
        })
      }

      // Отображаем точки
      if (points.length > 0) {
        const pointCollection = { type: 'FeatureCollection', features: points }
        map.addSource('custom-point-data', { type: 'geojson', data: pointCollection })
        map.addLayer({
          id: 'custom-point-layer',
          type: 'circle',
          source: 'custom-point-data',
          paint: {
            'circle-radius': [
              'match', ['get', 'type'],
              'start', 10,
              'end', 10,
              8
            ],
            'circle-color': [
              'match', ['get', 'type'],
              'start', '#4ade80',
              'end', '#e94560',
              '#00d4ff'
            ],
            'circle-stroke-width': 2,
            'circle-stroke-color': '#fff',
          },
        })
        map.on('click', 'custom-point-layer', (e) => {
          const props = e.features[0].properties
          const coords = e.features[0].geometry.coordinates
          
          let content = ''
          if (props.type === 'fire_station') {
            content = `
              <strong>🚒 ${props.name || 'Пожарная часть'}</strong>
              ${props.address ? `<div class="popup-row"><span class="popup-label">Адрес:</span> <span class="popup-value">${props.address}</span></div>` : ''}
              ${props.phone ? `<div class="popup-row"><span class="popup-label">Телефон:</span> <span class="popup-value">${props.phone}</span></div>` : ''}
              ${props.distance_km ? `<div class="popup-row"><span class="popup-label">Расстояние:</span> <span class="popup-value">${props.distance_km} км</span></div>` : ''}
            `
          } else if (props.type === 'start' || props.type === 'end') {
            const label = props.type === 'start' ? '🟢 Начало маршрута' : '🔴 Конец маршрута'
            content = `<strong>${label}</strong>`
            if (props.distance_km) {
              content += `<div class="popup-row"><span class="popup-label">Расстояние:</span> <span class="popup-value">${props.distance_km} км</span></div>`
            }
            if (props.duration_min) {
              content += `<div class="popup-row"><span class="popup-label">Время:</span> <span class="popup-value">${props.duration_min} мин</span></div>`
            }
          } else {
            content = `<strong>${props.name || 'Точка'}</strong>`
            // Показываем все дополнительные свойства
            Object.entries(props).forEach(([key, value]) => {
              if (!['name', 'type'].includes(key) && value) {
                content += `<div class="popup-row"><span class="popup-label">${key}:</span> <span class="popup-value">${value}</span></div>`
              }
            })
          }
          
          new maplibregl.Popup({ className: 'map-popup-container', closeButton: false })
            .setLngLat(coords)
            .setHTML(`<div class="map-popup">${content}</div>`)
            .addTo(map)
        })
      }

      // Отображаем полигоны
      if (polygons.length > 0) {
        const polygonCollection = { type: 'FeatureCollection', features: polygons }
        map.addSource('custom-polygon-data', { type: 'geojson', data: polygonCollection })
        map.addLayer({
          id: 'custom-polygon-layer',
          type: 'fill',
          source: 'custom-polygon-data',
          paint: {
            'fill-color': '#9d4edd',
            'fill-opacity': 0.5,
          },
        })
        map.addLayer({
          id: 'custom-polygon-outline',
          type: 'line',
          source: 'custom-polygon-data',
          paint: {
            'line-color': '#9d4edd',
            'line-width': 2,
            'line-opacity': 0.8,
          },
        })
      }

      // Auto-center на маршруте/полигоне/точке
      const firstFeature = geojson.features[0]
      if (firstFeature?.geometry) {
        if (firstFeature.geometry.type === 'Point') {
          map.flyTo({ center: firstFeature.geometry.coordinates, zoom: 10, duration: 1500 })
        } else {
          const bounds = new maplibregl.LngLatBounds()
          const extractCoords = (coord) => {
            if (typeof coord[0] === 'number') bounds.extend(coord)
            else coord.forEach(extractCoords)
          }
          extractCoords(firstFeature.geometry.coordinates)
          map.fitBounds(bounds, { padding: 50, maxZoom: 12, duration: 1500 })
        }
      }
    }
  }, [customData])

  return <div ref={mapContainerRef} style={{ width: '100%', height: '100%' }} />
}

export default MapView
