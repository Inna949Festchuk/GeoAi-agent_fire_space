import { useState, useEffect } from 'react'
import MapView from './components/MapView'
import ChatPanel from './components/ChatPanel'
import { fetchFires, fetchFiresFromFIRMS, fetchBurns, fetchStats } from './api/client'

function App() {
  const [fireData, setFireData] = useState(null)
  const [burnData, setBurnData] = useState(null)
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(false)
  const [bbox, setBbox] = useState(null)
  const [source, setSource] = useState('all')
  const [days, setDays] = useState(1)
  const [showFires, setShowFires] = useState(true)
  const [showBurns, setShowBurns] = useState(true)

  // Load fires and burns on initial mount
  useEffect(() => {
    const loadInitialData = async () => {
      setLoading(true)
      try {
        // Default bbox for Siberia (extended to include 79° lon)
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

  const handleSearch = async () => {
    setLoading(true)
    try {
      // Use current bbox or default to extended Siberia
      const searchBbox = bbox || [75, 55, 110, 75]
      // Fetch fresh data from NASA FIRMS
      const [fireResult, burnsResult, statsData] = await Promise.all([
        fetchFiresFromFIRMS({ bbox: searchBbox, source, days }),
        fetchBurns({ bbox: searchBbox }),
        fetchStats({ bbox: searchBbox, days }),
      ])
      setFireData(fireResult)
      setBurnData(burnsResult)
      setStats(statsData)
    } catch (err) {
      console.error('Search failed:', err)
    } finally {
      setLoading(false)
    }
  }

  const handleMapMove = (newBbox) => {
    setBbox(newBbox)
  }

  const handleChatResponse = async (mapDataList) => {
    if (mapDataList && mapDataList.length > 0) {
      // Обрабатываем каждый результат в зависимости от типа
      mapDataList.forEach(item => {
        if (item.type === 'burn') {
          setBurnData(item.data)
        } else {
          setFireData(item.data)
        }
      })
      
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
          showFires={showFires}
          showBurns={showBurns}
          onMapMove={handleMapMove}
        />
      </div>
      <div className="sidebar">
        <div className="sidebar-header">
          <h1>🔥 Fire Monitor</h1>
          <p>Мониторинг лесных пожаров из космоса</p>
        </div>

        <div className="controls">
          <div className="control-group">
            <label>Спутник</label>
            <select value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="all">Все</option>
              <option value="VIIRS_SNPP">VIIRS Suomi NPP</option>
              <option value="VIIRS_NOAA20">VIIRS NOAA-20</option>
              <option value="MODIS_Terra">MODIS Terra</option>
              <option value="MODIS_Aqua">MODIS Aqua</option>
            </select>
          </div>
          <div className="control-group">
            <label>Период</label>
            <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
              <option value={1}>24 часа</option>
              <option value={2}>2 дня</option>
              <option value={3}>3 дня</option>
              <option value={7}>7 дней</option>
              <option value={10}>10 дней</option>
            </select>
          </div>
          <div className="control-group">
            <label>&nbsp;</label>
            <button className="btn btn-primary" onClick={handleSearch} disabled={loading}>
              {loading ? 'Загрузка...' : 'Найти пожары'}
            </button>
          </div>
        </div>

        <div className="layer-toggles">
          <label className="layer-toggle">
            <input
              type="checkbox"
              checked={showFires}
              onChange={(e) => setShowFires(e.target.checked)}
            />
            Очаги пожаров
          </label>
          <label className="layer-toggle">
            <input
              type="checkbox"
              checked={showBurns}
              onChange={(e) => setShowBurns(e.target.checked)}
            />
            Гари
          </label>
        </div>

        {stats && (
          <div className="stats-panel">
            <div className="stat-item">
              <div className="stat-value">{stats.fires?.total || 0}</div>
              <div className="stat-label">Очагов</div>
            </div>
            <div className="stat-item">
              <div className="stat-value">{stats.fires?.high_confidence || 0}</div>
              <div className="stat-label">Высокая довер.</div>
            </div>
            <div className="stat-item">
              <div className="stat-value">{stats.burns?.total_area_ha || 0}</div>
              <div className="stat-label">Гари</div>
            </div>
          </div>
        )}

        <ChatPanel
          bbox={bbox}
          onResponse={handleChatResponse}
        />
      </div>
    </div>
  )
}

export default App
