import { useEffect, useRef } from 'react'
import maplibregl from 'maplibre-gl'

function MapView({ fireData, burnData, showFires, showBurns, onMapMove }) {
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

        new maplibregl.Popup({ className: 'fire-popup-container', closeButton: false })
          .setLngLat(coords)
          .setHTML(`
            <div class="fire-popup">
              <strong>Очаг пожара</strong><br/>
              Источник: ${props.source}<br/>
              Яркость: ${Number(props.brightness).toFixed(1)} K<br/>
              Уверенность: ${props.confidence}<br/>
              FRP: ${props.frp ? Number(props.frp).toFixed(1) + ' MW' : 'N/A'}<br/>
              Время: ${new Date(props.detected_at).toLocaleString('ru-RU')}
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

        new maplibregl.Popup({ className: 'burn-popup-container', closeButton: false })
          .setLngLat(coords)
          .setHTML(`
            <div class="burn-popup">
              <strong>Горевшая территория</strong><br/>
              Степень: ${severityLabels[props.severity] || props.severity}<br/>
              Площадь: ${Number(props.area_ha).toFixed(2)} га<br/>
              Средний dNBR: ${Number(props.mean_dnbr).toFixed(1)}<br/>
              Период: ${props.pre_date} — ${props.post_date}
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

  return <div ref={mapContainerRef} style={{ width: '100%', height: '100%' }} />
}

export default MapView
