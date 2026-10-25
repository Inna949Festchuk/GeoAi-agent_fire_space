import { useEffect, useRef, useMemo, useState } from 'react'
import maplibregl from 'maplibre-gl'
import { outlineIconSvg } from './OutlineIcons'

// Определения базовых карт. Каждая база даёт ПОЛНЫЙ стиль через buildStyle() —
// переключение выполняется map.setStyle (не заменой источника). Любой стиль
// ОБЯЗАН содержать 'dem' + 'dem-hillshade', иначе ломается 2.5D-рельеф.
// Растерный источник ESRI (спутниковая карта, те же тайлы, что и в flight-planning)
const ESRI_SOURCE = {
  type: 'raster',
  tiles: [
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  ],
  tileSize: 256,
  maxzoom: 19,
  attribution: 'Esri, Maxar, Earthstar Geographics',
}

const BASEMAPS = {
  // «Тёмная карта» — CARTO Dark Matter: ВЕКТОРНЫЙ стиль (MVT).
  // Легаси-растер CARTO (rastertiles/voyager, dark_all) теперь отдаёт заглушку
  // «API KEY REQUIRED» (проверено 2026-10-25: HTTP 200, но 2049-байтовый
  // placeholder-PNG) — поэтому старый вариант «Обычная карта (CartoDB)»
  // отображался пустым. Векторный CDN tiles.basemaps.cartocdn.com публичный,
  // ТОКЕН НЕ НУЖЕН (проверено: style.json 200 + MVT-тайл 200).
  carto: {
    label: 'Тёмная карта (CartoDB Dark Matter)',
    icon: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"><path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2z"/><path d="M9 4v14"/><path d="M15 6v14"/></svg>',
    buildStyle: async () => {
      const remote = await fetchDarkMatterStyle()
      // Мерджим свой рельеф: 'dem' + hillshade сразу после 'background'
      // (под всей картографией; тематические слои добавятся сверху)
      const merged = { ...remote, sources: { ...remote.sources, dem: DEM_SOURCE } }
      const firstNonBg = merged.layers.findIndex((l) => l.type !== 'background')
      const idx = firstNonBg === -1 ? 0 : firstNonBg
      merged.layers = [
        ...merged.layers.slice(0, idx),
        DEM_HILLSHADE_LAYER,
        ...merged.layers.slice(idx),
      ]
      return merged
    },
  },
  esri: {
    label: 'Спутниковая карта (ESRI World Imagery)',
    icon: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M3.6 9h16.8"/><path d="M3.6 15h16.8"/><path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18z"/></svg>',
    buildStyle: () => makeBaseStyle(),
  },
}

// Переключатель базовой карты — MapLibre-контрол в штатном виде
// (такой же белый блок, как NavigationControl). Встроенного селектора
// базовых карт в MapLibre GL нет, а runtime-класса Control — тоже
// (в отличие от Mapbox: в maplibre-gl 4.x `maplibregl.Control ===
// undefined`), поэтому — стандартный подход: обычный объект,
// реализующий интерфейс IControl (onAdd/onRemove).
// Контрол рисует кнопки и сообщает выбранный id (onSelect); сам стиль
// применяет React-эффект map.setStyle (см. эффект по basemapId в MapView):
// каждый базовый стиль — полный (векторный CARTO Dark Matter или растер
// ESRI + рельеф), а setStyle стирает тематические слои, которые эффект
// перерисовывает через styleEpoch.
const createBasemapSwitcherControl = (basemaps, defaultId, onSelect) => {
  let currentId = defaultId
  let container = null

  const refreshActive = () => {
    if (!container) return
    const btns = container.children
    let i = 0
    for (const id of Object.keys(basemaps)) {
      btns[i]?.classList.toggle('active', id === currentId)
      i += 1
    }
  }

  const select = (id) => {
    if (id === currentId) return
    currentId = id
    console.log('[basemap] switch to:', id, basemaps[id]?.label)
    refreshActive()
    onSelect?.(id)
  }

  return {
    onAdd(m) {
      container = document.createElement('div')
      container.className = 'maplibregl-ctrl maplibregl-ctrl-group basemap-switcher'

      for (const [id, bm] of Object.entries(basemaps)) {
        const btn = document.createElement('button')
        btn.className = 'map-std-btn'
        btn.title = bm.label
        btn.setAttribute('aria-label', bm.label)
        btn.innerHTML = bm.icon
        btn.addEventListener('click', () => select(id))
        container.appendChild(btn)
      }

      refreshActive()
      return container
    },
    onRemove() {
      if (container) container.remove()
      container = null
    },
  }
}

// 2.5D рельеф: terrarium-тайлы SRTM v3 (AWS Open Data, публичный бакет).
// Покрытие SRTM: 56°S–60°N — все наши демо-регионы (Сибирь, Красноярск) в пределах.
const TERRAIN = {
  sourceId: 'dem',
  tiles: ['https://elevation-tiles-prod.s3.amazonaws.com/terrarium/{z}/{x}/{y}.png'],
  encoding: 'terrarium',
  tileSize: 256,
  maxzoom: 13,
  exaggeration: 2.0,
}

const TERRAIN_ICON = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round"><path d="M3 20 L9.5 8 L13.5 14.5 L16 10.5 L21 20 Z"/><path d="M9.5 8 L11 10.5 L9.5 12 L8 10.5 Z" fill="currentColor" stroke="none"/></svg>'

// DEM (рельеф) — источник и слой hillshade, ОБЩИЕ для всех базовых стилей:
// после map.setStyle любой новый стиль обязан содержать 'dem', иначе
// 2.5D-рельеф ломается (DEM_SOURCE вшита и в makeBaseStyle, и в merge
// CARTO Dark Matter).
const DEM_SOURCE = {
  type: 'raster-dem',
  tiles: TERRAIN.tiles,
  encoding: TERRAIN.encoding,
  tileSize: TERRAIN.tileSize,
  maxzoom: TERRAIN.maxzoom,
  attribution: 'Elevation: SRTM v3 (AWS Open Data)',
}

const DEM_HILLSHADE_LAYER = {
  id: 'dem-hillshade',
  type: 'hillshade',
  source: 'dem',
  paint: {
    'hillshade-shadow-color': 'rgba(0, 0, 0, 0.4)',
    'hillshade-highlight-color': 'rgba(255, 255, 255, 0.3)',
    'hillshade-exaggeration': 0.7,
  },
}

// Базовый (стартовый) стиль карты: ESRI-спутник + рельеф
const makeBaseStyle = () => ({
  version: 8,
  sources: {
    basemap: ESRI_SOURCE,
    dem: DEM_SOURCE,
  },
  layers: [
    {
      id: 'basemap',
      type: 'raster',
      source: 'basemap',
      minzoom: 0,
      maxzoom: 19,
    },
    // Тени рельефа поверх базовой карты (работает и на ESRI, и на CARTO);
    // стоит ниже тематических слоёв — те добавляются поверх.
    DEM_HILLSHADE_LAYER,
  ],
})

// CARTO Dark Matter — публичный векторный стиль, ТОКЕН НЕ ТРЕБУЕТСЯ
// (легаси-растер CARTO требует ключ — см. комментарий у BASEMAPS.carto)
const CARTO_DARK_STYLE_URL = 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json'
let darkMatterCache = null
const fetchDarkMatterStyle = async () => {
  if (darkMatterCache) return darkMatterCache
  const r = await fetch(CARTO_DARK_STYLE_URL)
  if (!r.ok) throw new Error(`CARTO Dark Matter style: HTTP ${r.status}`)
  darkMatterCache = await r.json()
  return darkMatterCache
}

// Тоггл 2.5D-рельефа — штатный вид MapLibre-контрола (один белый блок
// с иконкой под переключателем базовых карт). Вкл/выкл переключает
// map.setTerrain(): с наклонением (pitch/rotation) карта становится объёмной.
// Слушатель styledata персистентный: после каждого map.setStyle (смена
// базовой карты) рельеф самовосстанавливается согласно текущему
// состоянию тумблера (setStyle может сбрасывать terrain).
const createTerrainToggleControl = (terrain) => {
  let on = true
  let container = null
  let map = null

  const syncTerrain = () => {
    if (!map || !map.getLayer('dem-hillshade')) return
    try {
      const has = !!map.getTerrain()
      if (on && !has) {
        map.setTerrain({ source: terrain.sourceId, exaggeration: terrain.exaggeration })
        console.log('[terrain] (re)applied, exaggeration', terrain.exaggeration)
      } else if (!on && has) {
        map.setTerrain(null)
        console.log('[terrain] off')
      }
    } catch (err) {
      console.error('[terrain] sync failed:', err)
    }
  }

  return {
    onAdd(m) {
      map = m
      container = document.createElement('div')
      container.className = 'maplibregl-ctrl maplibregl-ctrl-group terrain-toggle'
      const btn = document.createElement('button')
      btn.className = 'map-std-btn'
      btn.title = 'Рельеф 2.5D: вкл (SRTM)'
      btn.setAttribute('aria-label', 'Рельеф 2.5D')
      btn.innerHTML = TERRAIN_ICON
      btn.addEventListener('click', () => {
        on = !on
        btn.classList.toggle('active', on)
        btn.title = on ? 'Рельеф 2.5D: вкл (SRTM)' : 'Рельеф 2.5D: выкл'
        syncTerrain()
      })
      btn.classList.toggle('active', on)
      container.appendChild(btn)
      // Первое применение + авто-восстановление после каждой смены стиля
      map.on('styledata', syncTerrain)
      syncTerrain()
      return container
    },
    onRemove() {
      if (map) map.off('styledata', syncTerrain)
      if (container) container.remove()
      map = null
      container = null
    },
  }
}

function MapView({
  fireData, burnData, customData, validationData,
  showFires, showBurns, showRoutes, showFireStations, showCustom, showValidation,
  showValGt, showValStrela, showValBurn, showValModis, showValZone,
  onMapMove
}) {
  const mapRef = useRef(null)
  const mapContainerRef = useRef(null)
  const moveTimeoutRef = useRef(null)
  // Дatasets, на которые уже выполнена подгонка вида (fitBounds) — чтобы
  // повторное переключение слоя не «двигало» карту
  const validationFittedRef = useRef(null)
  // Последний набор гарей, под который выполнен fitBounds — чтобы
  // перерисовка после смены базовой карты (styleEpoch) не «прыгала» картой
  const burnFittedForRef = useRef(null)
  // Выбранная базовая карта; эффект ниже применяет её через map.setStyle
  const [basemapId, setBasemapId] = useState('esri')
  // Счётчик применённых базовых стилей: setStyle стирает тематические слои,
  // эффекты-перерисовщики зависят от него и добавляют слои заново
  const [styleEpoch, setStyleEpoch] = useState(0)
  // Базовая карта, реально применённая на карте (пропуск первичного и
  // повторного прогонов эффекта, напр. в StrictMode)
  const appliedBasemapRef = useRef('esri')
  // Event handlers слоя валидации — для корректного map.off при повторном
  // рендере (иначе попапы дублируются при каждом новом прогоне)
  const validationHandlersRef = useRef(null)
  // HTML-оверлеи (marker'ы) для текстовых подписей пользовательских точек —
  // гарантированно рендерят кириллицу (в отличие от symbol-слоёв, которым
  // нужен глиф-шрифт, отсутствующий в растровом OSM-стиле)
  const labelMarkersRef = useRef([])

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
      style: makeBaseStyle(),
      center: [95, 62], // Center on Siberia
      zoom: 4,
    })

    map.addControl(new maplibregl.NavigationControl(), 'top-left')
    // Штатного селектора базовых карт в MapLibre GL нет — свой контрол
    // в стандартном виде (белый блок под навигацией, слева сверху).
    // Выбор сообщает setBasemapId — стиль применяет эффект ниже.
    map.addControl(createBasemapSwitcherControl(BASEMAPS, 'esri', setBasemapId), 'top-left')
    // 2.5D-рельеф (SRTM terrarium + setTerrain) — кнопка под переключателем карт
    map.addControl(createTerrainToggleControl(TERRAIN), 'top-left')
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
      labelMarkersRef.current.forEach(m => m.remove())
      labelMarkersRef.current = []
      map.remove()
      mapRef.current = null
    }
  }, [])

  // Применение выбранной базовой карты (map.setStyle). Пропускаем прогоны,
  // где на карте уже эта база (стартовый 'esri', повторные прогоны,
  // напр. StrictMode). setStyle стирает тематические слои: ждём готовности
  // нового стиля (появление 'dem-hillshade') и бём styleEpoch — эффекты
  // слоёв перерисуют данные поверх новой базы.
  useEffect(() => {
    if (basemapId === appliedBasemapRef.current) return
    const map = mapRef.current
    const bm = BASEMAPS[basemapId]
    if (!map || !bm) return

    let cancelled = false
    let fallbackTimer = null
    const onStyleData = () => {
      if (cancelled || !map.getLayer('dem-hillshade')) return
      cancelled = true
      map.off('styledata', onStyleData)
      clearTimeout(fallbackTimer)
      appliedBasemapRef.current = basemapId
      setStyleEpoch((e) => e + 1)
    }
    const run = async () => {
      try {
        const style = await bm.buildStyle()
        if (cancelled) return
        map.on('styledata', onStyleData)
        map.setStyle(style)
        // Страховка, если styledata с dem-hillshade так и не пришёл
        fallbackTimer = setTimeout(() => {
          if (!cancelled) {
            cancelled = true
            map.off('styledata', onStyleData)
            appliedBasemapRef.current = basemapId
            setStyleEpoch((e) => e + 1)
          }
        }, 8000)
      } catch (err) {
        console.error(`[basemap] не удалось применить «${bm.label}»:`, err, '— остаёмся на текущей базе (повторный клик по кнопке повторит попытку)')
      }
    }
    run()
    return () => {
      cancelled = true
      clearTimeout(fallbackTimer)
      if (mapRef.current) mapRef.current.off('styledata', onStyleData)
    }
  }, [basemapId])

  // Вспомогательные функции для попапов пользовательских точек/подписей
  const escapeHtml = (s) => String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;')

  const popupContentFromProps = (props) => {
    if (!props) return ''
    if (props.popup) return props.popup // LLM может задать готовый HTML в properties.popup
    let content = `<strong>${escapeHtml(props.name || props.label || props.title || 'Точка')}</strong>`
    if (props.label && props.label !== props.name) {
      content += `<div class="popup-row">${escapeHtml(props.label)}</div>`
    }
    if (props.description) {
      content += `<div class="popup-row"><span class="popup-label">Описание:</span> <span class="popup-value">${escapeHtml(props.description)}</span></div>`
    }
    if (props.coordinates_text) {
      content += `<div class="popup-row"><span class="popup-label">Координаты:</span> <span class="popup-value">${escapeHtml(props.coordinates_text)}</span></div>`
    }
    return content
  }

  const showCustomPopup = (map, coords, props) => {
    new maplibregl.Popup({ className: 'map-popup-container', closeButton: false })
      .setLngLat(coords)
      .setHTML(`<div class="map-popup">${popupContentFromProps(props)}</div>`)
      .addTo(map)
  }

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
  }, [fireData, showFires, styleEpoch])

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

      // Auto-center on first burn polygon — только когда сам набор данных
      // изменился (при перерисовке после смены базы карта не «прыгает»)
      const firstFeature = burnData.features[0]
      if (firstFeature && firstFeature.geometry && burnFittedForRef.current !== burnData) {
        burnFittedForRef.current = burnData
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
  }, [burnData, showBurns, styleEpoch])

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
      let content = `<strong>${outlineIconSvg('route')} Маршрут</strong>`
      if (props.distance_km) content += `<div class="popup-row"><span class="popup-label">Расстояние:</span> <span class="popup-value">${props.distance_km} км</span></div>`
      if (props.duration_min) content += `<div class="popup-row"><span class="popup-label">Время:</span> <span class="popup-value">${props.duration_min} мин</span></div>`
      new maplibregl.Popup({ className: 'map-popup-container', closeButton: false })
        .setLngLat(e.lngLat)
        .setHTML(`<div class="map-popup">${content}</div>`)
        .addTo(map)
    })
  }, [routesData, showRoutes, styleEpoch])

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
      let content = `<strong>${outlineIconSvg('station')} ${props.name || 'Пожарная часть'}</strong>`
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
  }, [fireStationsData, showFireStations, styleEpoch])

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
    // Удаляем старые HTML-подписи
    labelMarkersRef.current.forEach(m => m.remove())
    labelMarkersRef.current = []

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

      // Попап по клику на маркер — берём properties.popup, иначе name/label/description
      map.on('click', 'custom-point-layer', (e) => {
        const feat = e.features && e.features[0]
        if (!feat) return
        showCustomPopup(map, feat.geometry.coordinates, feat.properties)
      })
      map.on('mouseenter', 'custom-point-layer', () => { map.getCanvas().style.cursor = 'pointer' })
      map.on('mouseleave', 'custom-point-layer', () => { map.getCanvas().style.cursor = '' })

      // Текстовые подписи точек с properties.label — через HTML-оверлеи
      // (maplibregl.Marker), т.к. растровый OSM-стиль не содержит глиф-шрифта
      // с кириллицей и symbol-слой молча не рендерит русские надписи.
      points.forEach((p) => {
        const label = p.properties?.label
        if (!label || p.geometry?.type !== 'Point') return
        const el = document.createElement('div')
        el.textContent = String(label).replace(/\n/g, ' ')
        el.style.cssText = [
          'background: rgba(0,0,0,0.75)',
          'color: #fff',
          'padding: 2px 6px',
          'border-radius: 4px',
          'font-size: 12px',
          'font-weight: 600',
          'white-space: nowrap',
          'border: 1px solid rgba(255,255,255,0.4)',
          'cursor: pointer',
          'text-align: center',
          'line-height: 1.3',
        ].join(';')
        el.addEventListener('click', (ev) => {
          ev.stopPropagation()
          showCustomPopup(map, p.geometry.coordinates, p.properties)
        })
        const marker = new maplibregl.Marker({
          element: el,
          anchor: 'top',
          offset: [0, 10],
        })
          .setLngLat(p.geometry.coordinates)
          .addTo(map)
        labelMarkersRef.current.push(marker)
      })
    }
  }, [otherCustomData, showCustom, styleEpoch])

  // Слой «Валидация» — результат run_validation из чата:
  // синий — Рослесхоз (GT), красный — детекции Стрела, оранжевый — гари
  // Стрела, зелёный — MCD64A1 (эталон), серый — промзоны
  // (фичи типизированы properties.type)
  useEffect(() => {
    const map = mapRef.current
    if (!map) return

    // ОЧИСТКА (слои + event handlers предыдущего рендера)
    const prevHandlers = validationHandlersRef.current
    if (prevHandlers) {
      Object.entries(prevHandlers).forEach(([evt, fn]) => map.off(evt, fn))
      validationHandlersRef.current = null
    }
    ;['validation-strela-points', 'validation-gt-points', 'validation-burns-outline',
      'validation-burns', 'validation-zones-outline', 'validation-zones',
      'validation-modis-outline'].forEach(id => {
      if (map.getLayer(id)) map.removeLayer(id)
    })
    if (map.getSource('validation-data')) map.removeSource('validation-data')

    const features = validationData?.features || []
    if (!showValidation || features.length === 0) return

    map.addSource('validation-data', { type: 'geojson', data: validationData })
    const byType = (t) => ['==', ['get', 'type'], t]

    // Промзоны (серые, внизу) — если включены
    if (showValZone) {
      map.addLayer({
        id: 'validation-zones',
        type: 'fill',
        source: 'validation-data',
        filter: byType('validation_zone'),
        paint: { 'fill-color': '#9ca3af', 'fill-opacity': 0.25 },
      })
      map.addLayer({
        id: 'validation-zones-outline',
        type: 'line',
        source: 'validation-data',
        filter: byType('validation_zone'),
        paint: { 'line-color': '#6b7280', 'line-width': 1, 'line-opacity': 0.6 },
      })
    }

    // Гари Стрела (оранжевые) — если включены
    if (showValBurn) {
      map.addLayer({
        id: 'validation-burns',
        type: 'fill',
        source: 'validation-data',
        filter: byType('validation_burn'),
        paint: { 'fill-color': '#ea580c', 'fill-opacity': 0.35 },
      })
      map.addLayer({
        id: 'validation-burns-outline',
        type: 'line',
        source: 'validation-data',
        filter: byType('validation_burn'),
        paint: { 'line-color': '#ea580c', 'line-width': 1.5, 'line-opacity': 0.8 },
      })
    }

    // NASA MCD64A1 — только граница (без заливки), если включён
    if (showValModis) {
      map.addLayer({
        id: 'validation-modis-outline',
        type: 'line',
        source: 'validation-data',
        filter: byType('validation_modis'),
        paint: { 'line-color': '#15803d', 'line-width': 2, 'line-opacity': 1 },
      })
    }

    // Рослесхоз GT (синие точки) — если включены
    if (showValGt) {
      map.addLayer({
        id: 'validation-gt-points',
        type: 'circle',
        source: 'validation-data',
        filter: byType('validation_gt'),
        paint: {
          'circle-radius': 5,
          'circle-color': '#3b82f6',
          'circle-stroke-width': 1.5,
          'circle-stroke-color': '#fff',
          'circle-opacity': 0.9,
        },
      })
    }

    // Детекции Стрела (красные точки) — если включены
    if (showValStrela) {
      map.addLayer({
        id: 'validation-strela-points',
        type: 'circle',
        source: 'validation-data',
        filter: byType('validation_strela'),
        paint: {
          'circle-radius': 6,
          'circle-color': '#ef4444',
          'circle-stroke-width': 1.5,
          'circle-stroke-color': '#fff',
        },
      })
    }

    // Подгонка вида под рамку валидации — один раз на набор данных
    if (validationFittedRef.current !== validationData) {
      validationFittedRef.current = validationData
      const b = new maplibregl.LngLatBounds()
      const ext = (c) => {
        if (!Array.isArray(c)) return
        if (typeof c[0] === 'number') b.extend(c)
        else c.forEach(ext)
      }
      features.forEach(f => { if (f.geometry) ext(f.geometry.coordinates) })
      if (!b.isEmpty()) {
        map.fitBounds(b, {
          padding: 60,
          maxZoom: 9,
          duration: 1200,
          easing: (t) => t * (2 - t),
        })
      }
    }

    const typeLabels = {
      validation_gt: 'Рослесхоз (GT)',
      validation_strela: 'Детекция Стрела',
      validation_burn: 'Гари Стрела',
      validation_modis: 'NASA MCD64A1 (выгорание, эталон)',
      validation_zone: 'Промышленная зона',
      validation_fp: 'Ложное срабатывание (FP)',
      validation_fn: 'Пропущенный (FN)',
    }
    const showValPopup = (e) => {
      const f = e.features && e.features[0]
      if (!f) return
      const p = f.properties || {}
      let rows = ''
      Object.entries(p).forEach(([k, v]) => {
        if (k === 'type' || v === null || v === undefined || v === '') return
        let val = v
        if (typeof val === 'number' && !Number.isInteger(val)) val = val.toFixed(2)
        rows += `<div class="popup-row"><span class="popup-label">${escapeHtml(k)}:</span> <span class="popup-value">${escapeHtml(String(val))}</span></div>`
      })
      new maplibregl.Popup({ className: 'map-popup-container', closeButton: false })
        .setLngLat(e.lngLat)
        .setHTML(`<div class="map-popup"><strong>${escapeHtml(typeLabels[p.type] || 'Валидация')}</strong>${rows}</div>`)
        .addTo(map)
    }
    const valOnEnter = () => { map.getCanvas().style.cursor = 'pointer' }
    const valOnLeave = () => { map.getCanvas().style.cursor = '' }
    validationHandlersRef.current = {
      click: showValPopup,
      mouseenter: valOnEnter,
      mouseleave: valOnLeave,
    }
    ;['validation-gt-points', 'validation-strela-points', 'validation-burns',
      'validation-modis', 'validation-zones'].forEach(id => {
      map.on('click', id, showValPopup)
      map.on('mouseenter', id, valOnEnter)
      map.on('mouseleave', id, valOnLeave)
    })
  }, [validationData, showValidation, showValGt, showValStrela, showValBurn, showValModis, showValZone, styleEpoch])

  return (
    <div style={{ width: '100%', height: '100%', position: 'relative' }}>
      <div ref={mapContainerRef} style={{ width: '100%', height: '100%' }} />
    </div>
  )
}

export default MapView
