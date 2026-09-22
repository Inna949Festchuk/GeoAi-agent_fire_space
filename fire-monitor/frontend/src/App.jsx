import { useState, useEffect, useRef, useCallback } from 'react'
import MapView from './components/MapView'
import ChatPanel from './components/ChatPanel'
import LayerPanel from './components/LayerPanel'
import { fetchFires, fetchBurns, fetchStats } from './api/client'

// Ключи для localStorage
const STORAGE_KEYS = {
  SIDEBAR_WIDTH: 'fireMonitor_sidebarWidth',
  SHOW_FIRES: 'fireMonitor_showFires',
  SHOW_BURNS: 'fireMonitor_showBurns',
  SHOW_ROUTES: 'fireMonitor_showRoutes',
  SHOW_STATIONS: 'fireMonitor_showFireStations',
  SHOW_CUSTOM: 'fireMonitor_showCustom',
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
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(false)
  const [bbox, setBbox] = useState(null)

  // Состояния видимости слоёв (с сохранением в localStorage)
  const [showFires, setShowFires] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_FIRES, true))
  const [showBurns, setShowBurns] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_BURNS, true))
  const [showRoutes, setShowRoutes] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_ROUTES, true))
  const [showFireStations, setShowFireStations] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_STATIONS, true))
  const [showCustom, setShowCustom] = useState(() => getStoredBool(STORAGE_KEYS.SHOW_CUSTOM, true))

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

  // Обработка команд управления слоями от AI-агента
  const handleLayerAction = (action) => {
    const { action: actionType, layer } = action
    
    const layerSetters = {
      fires: setShowFires,
      burns: setShowBurns,
      routes: setShowRoutes,
      fire_stations: setShowFireStations,
      custom: setShowCustom,
    }
    
    const layerStates = {
      fires: showFires,
      burns: showBurns,
      routes: showRoutes,
      fire_stations: showFireStations,
      custom: showCustom,
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
      // Собираем все custom features в один массив
      const allCustomFeatures = []
      
      // Обрабатываем каждый результат в зависимости от типа
      mapDataList.forEach(item => {
        if (item.type === 'burn') {
          setBurnData(item.data)
        } else if (item.type === 'custom') {
          // Собираем все features из custom данных
          if (item.data?.features) {
            allCustomFeatures.push(...item.data.features)
          } else if (item.data?.type === 'Feature') {
            allCustomFeatures.push(item.data)
          }
        } else {
          setFireData(item.data)
        }
      })
      
      // Устанавливаем объединённые custom данные
      if (allCustomFeatures.length > 0) {
        setCustomData({
          type: 'FeatureCollection',
          features: allCustomFeatures
        })
      }

      // Update stats after chat response
      const searchBbox = bbox || [75, 55, 110, 75]
      try {
        const statsData = await fetchStats({ bbox: searchBbox, days })
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
          showFires={showFires}
          showBurns={showBurns}
          showRoutes={showRoutes}
          showFireStations={showFireStations}
          showCustom={showCustom}
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
          <h1>🔥 Fire Monitor</h1>
          <p>Мониторинг лесных пожаров из космоса</p>
        </div>

        <LayerPanel
          showFires={showFires} setShowFires={setShowFires}
          showBurns={showBurns} setShowBurns={setShowBurns}
          showRoutes={showRoutes} setShowRoutes={setShowRoutes}
          showFireStations={showFireStations} setShowFireStations={setShowFireStations}
          showCustom={showCustom} setShowCustom={setShowCustom}
          hasFires={fireData?.features?.length > 0}
          hasBurns={burnData?.features?.length > 0}
          hasRoutes={customData?.features?.some(f => f.geometry?.type === 'LineString')}
          hasFireStations={customData?.features?.some(f => f.properties?.type === 'fire_station')}
          hasCustom={customData?.features?.length > 0}
        />

        <ChatPanel
          bbox={bbox}
          onResponse={handleChatResponse}
          onLayerAction={handleLayerAction}
        />
      </div>
    </div>
  )
}

export default App
