import { useState, useEffect, useRef, useCallback } from 'react'
import MapView from './components/MapView'
import ChatPanel from './components/ChatPanel'
import LayerPanel from './components/LayerPanel'
import { FlameIcon } from './components/OutlineIcons'
import { fetchFires, fetchBurns, fetchStats, fetchLatestBurnJob } from './api/client'

// Ключи для localStorage
const STORAGE_KEYS = {
  SIDEBAR_WIDTH: 'fireMonitor_sidebarWidth',
  SHOW_FIRES: 'fireMonitor_showFires',
  SHOW_BURNS: 'fireMonitor_showBurns',
  SHOW_ROUTES: 'fireMonitor_showRoutes',
  SHOW_STATIONS: 'fireMonitor_showFireStations',
  SHOW_CUSTOM: 'fireMonitor_showCustom',
  SHOW_VALIDATION: 'fireMonitor_showValidation',
  // Подслои валидации
  SHOW_VAL_GT: 'fireMonitor_valGt',
  SHOW_VAL_STRELA: 'fireMonitor_valStrela',
  SHOW_VAL_BURN: 'fireMonitor_valBurn',
  SHOW_VAL_MODIS: 'fireMonitor_valModis',
  SHOW_VAL_ZONE: 'fireMonitor_valZone',
}

// Утиита для чтения из localStorage
const getStoredBool = (key, defaultValue) => {
  const stored = localStorage.getItem(key)
  return stored !== null ? stored === 'true' : defaultValue
}

const getStoredNumber = (key, defaultValue, min, max) => {
  const stored = localStorage.getItem(key)
  if (stored !== null) {
    const num = parseInt(stored, 10)
    if (!isNaN(num) && num >= min && num <= max) return num
  }
  return defaultValue
}

function App() {
  const [fireData, setFireData] = useState(null)
  const [burnData, setBurnData] = useState(null)
  const [customData, setCustomData] = useState(null)
  const [validationData, setValidationData] = useState(null)
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(false)
  const [bbox, setBbox] = useState(null)
  const [chatBusy, setChatBusy] = useState(false)
  const [burnJob, setBurnJob] = useState(null)

  // Состояния видимости слоёв (с сохранением в localStorage)
  const [showFires, setShowFires] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_FIRES, true))
  const [showBurns, setShowBurns] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_BURNS, true))
  const [showRoutes, setShowRoutes] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_ROUTES, true))
  const [showFireStations, setShowFireStations] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_STATIONS, true))
  const [showCustom, setShowCustom] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_CUSTOM, true))
  const [showValidation, setShowValidation] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_VALIDATION, true))
  // Подслои валидации
  const [showValGt, setShowValGt] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_VAL_GT, true))
  const [showValStrela, setShowValStrela] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_VAL_STRELA, true))
  const [showValBurn, setShowValBurn] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_VAL_BURN, true))
  const [showValModis, setShowValModis] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_VAL_MODIS, true))
  const [showValZone, setShowValZone] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_VAL_ZONE, true))

  // Resizable sidebar
  const [sidebarWidth, setSidebarWidth] = useState(() => 
    getStoredNumber(STORAGE_KEYS.SIDEBAR_WIDTH, 570, 300, 800)
  )
  const [isResizing, setIsResizing] = useState(false)
  const resizeRef = useRef(null)

  // Сохранение состояния слоёв в localStorage
  useEffect(() => { localStorage.setItem(STORAGE_KEYS.SHOW_FIRES, showFires) }, [showFires])
  useEffect(() => { localStorage.setItem(STORAGE_KEYS.SHOW_BURNS, showBurns) }, [showBurns])
  useEffect(() => { localStorage.setItem(STORAGE_KEYS.SHOW_ROUTES, showRoutes) }, [showRoutes])
  useEffect(() => { localStorage.setItem(STORAGE_KEYS.SHOW_STATIONS, showFireStations) }, [showFireStations])
  useEffect(() => { localStorage.setItem(STORAGE_KEYS.SHOW_CUSTOM, showCustom) }, [showCustom])
  useEffect(() => { localStorage.setItem(STORAGE_KEYS.SHOW_VALIDATION, showValidation) }, [showValidation])
  // Подслои валидации
  useEffect(() => { localStorage.setItem(STORAGE_KEYS.SHOW_VAL_GT, showValGt) }, [showValGt])
  useEffect(() => { localStorage.setItem(STORAGE_KEYS.SHOW_VAL_STRELA, showValStrela) }, [showValStrela])
  useEffect(() => { localStorage.setItem(STORAGE_KEYS.SHOW_VAL_BURN, showValBurn) }, [showValBurn])
  useEffect(() => { localStorage.setItem(STORAGE_KEYS.SHOW_VAL_MODIS, showValModis) }, [showValModis])
  useEffect(() => { localStorage.setItem(STORAGE_KEYS.SHOW_VAL_ZONE, showValZone) }, [showValZone])

  // Сохранение ширины sidebar
  useEffect(() => { localStorage.setItem(STORAGE_KEYS.SIDEBAR_WIDTH, sidebarWidth) }, [sidebarWidth])

  // Resize handlers
  const handleMouseDown = useCallback((e) => {
    e.preventDefault()
    setIsResizing(true)
  }, [])

  const handleMouseMove = useCallback((e) => {
    if (!isResizing) return
    const newWidth = window.innerWidth - e.clientX
    const clampedWidth = Math.max(300, Math.min(800, newWidth))
    setSidebarWidth(clampedWidth)
  }, [isResizing])

  const handleMouseUp = useCallback(() => {
    setIsResizing(false)
  }, [])

  useEffect(() => {
    if (isResizing) {
      document.addEventListener('mousemove', handleMouseMove)
      document.addEventListener('mouseup', handleMouseUp)
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
    } else {
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
    return () => {
      document.removeEventListener('mousemove', handleMouseMove)
      document.removeEventListener('mouseup', handleMouseUp)
    }
  }, [isResizing, handleMouseMove, handleMouseUp])

  // Load fires and burns on initial mount
  useEffect(() => {
    const loadInitialData = async () => {
      setLoading(true)
      try {
        const defaultBbox = [75, 55, 110, 75]
        const [fires, burns, statsData] = await Promise.all([
          fetchFires({ bbox: defaultBbox, source: 'all', days: 1 }),
          fetchBurns({ bbox: defaultBbox }),
          fetchStats({ bbox: defaultBbox, days: 1 }),
        ])
        setFireData(fires)
        setBurnData(burns)
        setStats(statsData)
        setBbox(defaultBbox)
      } catch (err) {
        console.error('Initial load failed:', err)
      } finally {
        setLoading(false)
      }
    }
    loadInitialData()
  }, [])

  const handleMapMove = (newBbox) => {
    setBbox(newBbox)
  }

  // Пока чат обрабатывает запрос (мульти-тайловое картирование гарей идёт
  // несколько минут), периодически обновляем слой гарей из БД и прогресс
  // джобы — полигоны появляются на карте по мере готовности тайлов.
  useEffect(() => {
    if (!chatBusy) return
    let cancelled = false
    const poll = async () => {
      try {
        const searchBbox = bbox || [75, 55, 110, 75]
        const [burns, job] = await Promise.all([
          fetchBurns({ bbox: searchBbox }),
          fetchLatestBurnJob(),
        ])
        if (cancelled) return
        setBurnData(burns)
        setBurnJob(job)
      } catch (err) {
        console.error('Burn progress poll failed:', err)
      }
    }
    const timeout = setTimeout(poll, 2000)
    const interval = setInterval(poll, 15000)
    return () => {
      cancelled = true
      clearTimeout(timeout)
      clearInterval(interval)
    }
  }, [chatBusy, bbox])

  const handleChatStateChange = useCallback((busy) => {
    setChatBusy(busy)
    if (!busy) setBurnJob(null)
  }, [])

  // Обработка команд управления слоями от AI-агента
  const handleLayerAction = (action) => {
    const { action: actionType, layer } = action
    
    const layerSetters = {
      fires: setShowFires,
      burns: setShowBurns,
      routes: setShowRoutes,
      fire_stations: setShowFireStations,
      custom: setShowCustom,
      validation: setShowValidation,
    }

    const layerStates = {
      fires: showFires,
      burns: showBurns,
      routes: showRoutes,
      fire_stations: showFireStations,
      custom: showCustom,
      validation: showValidation,
    }
    
    const setter = layerSetters[layer]
    if (!setter) return
    
    if (actionType === 'show') {
      setter(true)
    } else if (actionType === 'hide') {
      setter(false)
    } else if (actionType === 'toggle') {
      setter(!layerStates[layer])
    }
  }

  const handleChatResponse = async (mapDataList) => {
    if (mapDataList && mapDataList.length > 0) {
      // Собираем новые custom features из текущего ответа
      const newCustomFeatures = []

      // Обрабатываем каждый результат в зависимости от типа
      mapDataList.forEach(item => {
        if (item.type === 'burn') {
          setBurnData(item.data)
        } else if (item.type === 'fire') {
          setFireData(item.data)
        } else if (item.type === 'validation') {
          // Результат run_validation: отдельный слой «Валидация»
          // (GT / детекции / гари / промзоны типизированы properties.type)
          setValidationData(item.data)
        } else {
          // custom / routes / fire_stations: MapView сам разделит features
          // на слои маршрутов, станций и прочего по геометрии и properties.type
          if (item.data?.features) {
            newCustomFeatures.push(...item.data.features)
          } else if (item.data?.type === 'Feature') {
            newCustomFeatures.push(item.data)
          }
        }
      })

      // Добавляем новые features к существующим (не заменяем!)
      // Это позволяет сохранять маршруты при добавлении подписей и других элементов
      if (newCustomFeatures.length > 0) {
        setCustomData(prev => {
          const existingFeatures = prev?.features || []
          return {
            type: 'FeatureCollection',
            features: [...existingFeatures, ...newCustomFeatures]
          }
        })
      }

      // Update stats after chat response
      const searchBbox = bbox || [75, 55, 110, 75]
      try {
        // days: 1 — как в начальном загрузке (строка loadInitialData);
        // fetchStats без days не ставил бы фильтр date_from
        const statsData = await fetchStats({ bbox: searchBbox, days: 1 })
        setStats(statsData)
      } catch (err) {
        console.error('Failed to update stats:', err)
      }
    }
  }

  return (
    <div className="app-layout">
      <div className="map-container">
        <MapView
          fireData={fireData}
          burnData={burnData}
          customData={customData}
          validationData={validationData}
          showFires={showFires}
          showBurns={showBurns}
          showRoutes={showRoutes}
          showFireStations={showFireStations}
          showCustom={showCustom}
          showValidation={showValidation}
          showValGt={showValGt}
          showValStrela={showValStrela}
          showValBurn={showValBurn}
          showValModis={showValModis}
          showValZone={showValZone}
          onMapMove={handleMapMove}
        />
      </div>
      <div 
        className="sidebar" 
        style={{ width: `${sidebarWidth}px` }}
      >
        {/* Resize handle */}
        <div 
          ref={resizeRef}
          className="sidebar-resize-handle"
          onMouseDown={handleMouseDown}
        />
        
        <div className="sidebar-header">
          <h1><FlameIcon size={20} /> Fire Monitor</h1>
          <p>Мониторинг лесных пожаров из космоса</p>
        </div>

        <LayerPanel
          showFires={showFires} setShowFires={setShowFires}
          showBurns={showBurns} setShowBurns={setShowBurns}
          showRoutes={showRoutes} setShowRoutes={setShowRoutes}
          showFireStations={showFireStations} setShowFireStations={setShowFireStations}
          showCustom={showCustom} setShowCustom={setShowCustom}
          showValidation={showValidation} setShowValidation={setShowValidation}
          showValGt={showValGt} setShowValGt={setShowValGt}
          showValStrela={showValStrela} setShowValStrela={setShowValStrela}
          showValBurn={showValBurn} setShowValBurn={setShowValBurn}
          showValModis={showValModis} setShowValModis={setShowValModis}
          showValZone={showValZone} setShowValZone={setShowValZone}
          hasFires={fireData?.features?.length > 0}
          hasBurns={burnData?.features?.length > 0}
          hasRoutes={customData?.features?.some(f => f.geometry?.type === 'LineString')}
          hasFireStations={customData?.features?.some(f => f.properties?.type === 'fire_station')}
          hasCustom={customData?.features?.length > 0}
          hasValidation={validationData?.features?.length > 0}
        />

        <ChatPanel
          bbox={bbox}
          onResponse={handleChatResponse}
          onLayerAction={handleLayerAction}
          onChatStateChange={handleChatStateChange}
          jobProgress={burnJob}
        />
      </div>
    </div>
  )
}

export default App
