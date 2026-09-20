import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip, Legend } from 'recharts'
import { exportReport } from '../api/client'

function ResultsVisualization({ actions }) {
  if (!actions || actions.length === 0) return null

  const exportToGeoJSON = (data, filename) => {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  const exportToCSV = (data, filename) => {
    const csv = convertToCSV(data)
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  const handleExportReport = async (reportId, format) => {
    try {
      const data = await exportReport(reportId, format)

      if (format === 'csv') {
        const url = URL.createObjectURL(data)
        const a = document.createElement('a')
        a.href = url
        a.download = `burn_report_${reportId}.csv`
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        URL.revokeObjectURL(url)
      } else {
        exportToGeoJSON(data, `burn_report_${reportId}.geojson`)
      }
    } catch (error) {
      console.error('Export failed:', error)
      alert('Ошибка при экспорте отчёта')
    }
  }

  const convertToCSV = (data) => {
    if (!data || !data.features) return ''

    // Определяем тип данных: пожары (точки) или гари (полигоны)
    const firstFeature = data.features[0]
    const isPoint = firstFeature?.geometry?.type === 'Point'
    
    if (isPoint) {
      // Экспорт пожаров (точки)
      const headers = ['id', 'latitude', 'longitude', 'brightness', 'confidence', 'frp', 'source', 'detected_at']
      const rows = data.features.map((f, idx) => {
        const props = f.properties || {}
        const coords = f.geometry?.coordinates || [0, 0]
        return [
          idx + 1,
          coords[1],
          coords[0],
          props.brightness || '',
          props.confidence || '',
          props.frp || '',
          props.source || '',
          props.detected_at || ''
        ].join(',')
      })
      return [headers.join(','), ...rows].join('\n')
    } else {
      // Экспорт гарей (полигоны)
      const headers = ['id', 'severity', 'area_ha', 'dnbr_mean', 'pre_date', 'post_date', 'satellite', 'tile_id', 'geometry_wkt']
      const rows = data.features.map((f, idx) => {
        const props = f.properties || {}
        // Конвертируем геометрию в WKT формат
        const geometry = f.geometry
        let wkt = ''
        if (geometry?.type === 'Polygon' || geometry?.type === 'MultiPolygon') {
          wkt = `"${geometry.type}(${JSON.stringify(geometry.coordinates)})"`
        }
        return [
          idx + 1,
          props.severity || '',
          props.area_ha || '',
          props.dnbr_mean || '',
          props.pre_date || '',
          props.post_date || '',
          props.satellite || '',
          props.tile_id || '',
          wkt
        ].join(',')
      })
      return [headers.join(','), ...rows].join('\n')
    }
  }

  // Определяем тип данных для экспорта на основе действий
  const getExportType = () => {
    const hasBurns = actions.some(action => action.tool === 'map_burn_area')
    const hasFires = actions.some(action => action.tool === 'search_fires' || action.tool === 'get_fire_statistics')
    
    if (hasBurns && !hasFires) return 'burns'
    if (hasFires && !hasBurns) return 'fires'
    return 'both' // Если есть и то, и другое
  }

  // Загрузка полных данных из API для экспорта
  const loadFullData = async () => {
    const exportType = getExportType()
    
    try {
      if (exportType === 'burns' || exportType === 'both') {
        // Загружаем гари
        const response = await fetch('/api/burns/?page_size=10000')
        if (!response.ok) throw new Error('Failed to load burns data')
        const data = await response.json()
        return data.results || data
      } else {
        // Загружаем пожары
        const response = await fetch('/api/fires/?page_size=10000')
        if (!response.ok) throw new Error('Failed to load fires data')
        const data = await response.json()
        return data.results || data
      }
    } catch (error) {
      console.error('Error loading full data:', error)
      return null
    }
  }

  const handleExportGeoJSON = async () => {
    const fullData = await loadFullData()
    if (fullData) {
      exportToGeoJSON(fullData, `fire-monitor-export-${Date.now()}.geojson`)
    }
  }

  const handleExportCSV = async () => {
    const fullData = await loadFullData()
    if (fullData) {
      exportToCSV(fullData, `fire-monitor-export-${Date.now()}.csv`)
    }
  }

  const renderFireSearchChart = (action) => {
    const { result_summary } = action
    if (!result_summary) return null

    const data = [
      { name: 'Подтверждено', value: result_summary.total_fires || 0, color: '#4ade80' },
      { name: 'Отфильтровано', value: result_summary.filtered_out || 0, color: '#94a3b8' }
    ]

    return (
      <div className="result-chart">
        <h4>🔥 Поиск пожаров</h4>
        <ResponsiveContainer width="100%" height={200}>
          <PieChart>
            <Pie
              data={data}
              cx="50%"
              cy="50%"
              innerRadius={40}
              outerRadius={70}
              paddingAngle={2}
              dataKey="value"
            >
              {data.map((entry, index) => (
                <Cell key={`cell-${index}`} fill={entry.color} />
              ))}
            </Pie>
            <Tooltip />
            <Legend />
          </PieChart>
        </ResponsiveContainer>
        <div className="chart-stats">
          <div>Всего: <strong>{(result_summary.total_fires || 0) + (result_summary.filtered_out || 0)}</strong></div>
          <div>Процент фильтрации: <strong>{result_summary.filter_summary?.filter_rate?.toFixed(1) || 0}%</strong></div>
        </div>
      </div>
    )
  }

  const renderFireStatisticsChart = (action) => {
    const { result_summary } = action
    if (!result_summary) return null

    // Данные по источникам
    const sourceData = Object.entries(result_summary.by_source || {}).map(([name, value]) => ({
      name: name.replace('VIIRS_', '').replace('MODIS_', ''),
      value,
      color: name.includes('SNPP') ? '#e94560' : name.includes('NOAA') ? '#ff8c42' : '#ffd166'
    }))

    // Данные по уверенности
    const confidenceData = Object.entries(result_summary.by_confidence || {}).map(([name, value]) => ({
      name: name === 'high' ? 'Высокая' : name === 'nominal' ? 'Номинальная' : 'Низкая',
      value,
      color: name === 'high' ? '#e94560' : name === 'nominal' ? '#ff8c42' : '#4ade80'
    }))

    return (
      <div className="result-chart">
        <h4>📈 Статистика пожаров</h4>
        <div className="charts-row">
          <div className="mini-chart">
            <div className="chart-title">По источникам</div>
            <ResponsiveContainer width="100%" height={150}>
              <PieChart>
                <Pie
                  data={sourceData}
                  cx="50%"
                  cy="50%"
                  outerRadius={50}
                  dataKey="value"
                >
                  {sourceData.map((entry, index) => (
                    <Cell key={`cell-${index}`} fill={entry.color} />
                  ))}
                </Pie>
                <Tooltip />
                <Legend />
              </PieChart>
            </ResponsiveContainer>
          </div>
          <div className="mini-chart">
            <div className="chart-title">По уверенности</div>
            <ResponsiveContainer width="100%" height={150}>
              <PieChart>
                <Pie
                  data={confidenceData}
                  cx="50%"
                  cy="50%"
                  outerRadius={50}
                  dataKey="value"
                >
                  {confidenceData.map((entry, index) => (
                    <Cell key={`cell-${index}`} fill={entry.color} />
                  ))}
                </Pie>
                <Tooltip />
                <Legend />
              </PieChart>
            </ResponsiveContainer>
          </div>
        </div>
        <div className="chart-stats">
          <div>Период: <strong>{result_summary.days || 0} дн.</strong></div>
          <div>Подтверждено: <strong>{result_summary.total_valid || 0}</strong></div>
        </div>
      </div>
    )
  }

  const renderBurnMappingChart = (action) => {
    const { result_summary } = action
    if (!result_summary || result_summary.error) return null

    const severityData = Object.entries(result_summary.by_severity || {})
      .filter(([key]) => key !== 'unburned')
      .map(([name, data]) => ({
        name: name === 'high' ? 'Высокая' : name === 'moderate' ? 'Средняя' : 'Низкая',
        value: data.area_ha || 0,
        color: name === 'high' ? '#dc2626' : name === 'moderate' ? '#ea580c' : '#ca8a04'
      }))

    return (
      <div className="result-chart">
        <h4>🗺️ Карта гарей</h4>
        <ResponsiveContainer width="100%" height={200}>
          <PieChart>
            <Pie
              data={severityData}
              cx="50%"
              cy="50%"
              innerRadius={40}
              outerRadius={70}
              paddingAngle={2}
              dataKey="value"
            >
              {severityData.map((entry, index) => (
                <Cell key={`cell-${index}`} fill={entry.color} />
              ))}
            </Pie>
            <Tooltip formatter={(value) => `${value.toFixed(2)} га`} />
            <Legend />
          </PieChart>
        </ResponsiveContainer>
        <div className="chart-stats">
          <div>Общая площадь: <strong>{result_summary.total_burned_ha?.toFixed(2) || 0} га</strong></div>
          <div>Pre-fire: <strong>{result_summary.pre_date || 'N/A'}</strong></div>
          <div>Post-fire: <strong>{result_summary.post_date || 'N/A'}</strong></div>
        </div>
      </div>
    )
  }

  const renderBurnReportChart = (action) => {
    const { result_summary } = action
    if (!result_summary || result_summary.error) return null

    const severityDist = result_summary.severity_distribution || {}
    const severityData = Object.entries(severityDist)
      .filter(([key]) => key !== 'unburned')
      .map(([name, data]) => ({
        name: name === 'high' ? 'Высокая' : name === 'moderate' ? 'Средняя' : 'Низкая',
        value: data.area_ha || 0,
        percentage: data.percentage || 0,
        color: name === 'high' ? '#dc2626' : name === 'moderate' ? '#ea580c' : '#ca8a04'
      }))

    const totalArea = result_summary.total_burn_area_ha || 0
    const polygonsCount = result_summary.burn_polygons_count || 0
    const reportId = result_summary.report_id || ''

    return (
      <div className="result-chart burn-report">
        <h4>📊 Аналитический отчёт</h4>
        <div className="report-id">ID: {reportId}</div>

        {severityData.length > 0 && (
          <ResponsiveContainer width="100%" height={200}>
            <PieChart>
              <Pie
                data={severityData}
                cx="50%"
                cy="50%"
                innerRadius={40}
                outerRadius={70}
                paddingAngle={2}
                dataKey="value"
              >
                {severityData.map((entry, index) => (
                  <Cell key={`cell-${index}`} fill={entry.color} />
                ))}
              </Pie>
              <Tooltip formatter={(value) => `${value.toFixed(2)} га`} />
              <Legend />
            </PieChart>
          </ResponsiveContainer>
        )}

        <div className="chart-stats">
          <div>Общая площадь: <strong>{totalArea.toFixed(2)} га</strong></div>
          <div>Полигонов: <strong>{polygonsCount}</strong></div>
          {severityDist.high && (
            <div>Высокая тяжесть: <strong>{severityDist.high.percentage?.toFixed(1) || 0}%</strong></div>
          )}
          {severityDist.moderate && (
            <div>Средняя тяжесть: <strong>{severityDist.moderate.percentage?.toFixed(1) || 0}%</strong></div>
          )}
          {severityDist.low && (
            <div>Низкая тяжесть: <strong>{severityDist.low.percentage?.toFixed(1) || 0}%</strong></div>
          )}
        </div>

        <div className="report-actions">
          <button
            className="btn btn-small btn-export"
            onClick={() => handleExportReport(reportId, 'geojson')}
          >
            Экспорт GeoJSON
          </button>
          <button
            className="btn btn-small btn-export"
            onClick={() => handleExportReport(reportId, 'csv')}
          >
            Экспорт CSV
          </button>
        </div>
      </div>
    )
  }

  const renderSentinel2Chart = (action) => {
    const { result_summary } = action
    if (!result_summary) return null

    const scenesCount = result_summary.scenes_found || 0

    return (
      <div className="result-chart">
        <h4>🛰️ Сцены Sentinel-2</h4>
        <div className="chart-stats">
          <div>Найдено сцен: <strong>{scenesCount}</strong></div>
        </div>
      </div>
    )
  }

  // Проверяем наличие данных для экспорта
  const hasData = actions.length > 0

  return (
    <div className="results-visualization">
      <div className="charts-container">
        {actions.map((action, idx) => {
          if (action.tool === 'search_fires') return renderFireSearchChart(action)
          if (action.tool === 'get_fire_statistics') return renderFireStatisticsChart(action)
          if (action.tool === 'map_burn_area') return renderBurnMappingChart(action)
          if (action.tool === 'generate_burn_report') return renderBurnReportChart(action)
          if (action.tool === 'search_sentinel2_scenes') return renderSentinel2Chart(action)
          return null
        })}
      </div>
      
      {hasData && (
        <div className="export-buttons">
          <div className="export-label">Выгрузить результат в:</div>
          <button
            className="btn btn-export btn-geojson"
            onClick={handleExportGeoJSON}
          >
            GeoJSON
          </button>
          <button
            className="btn btn-export btn-csv"
            onClick={handleExportCSV}
          >
            CSV
          </button>
        </div>
      )}
    </div>
  )
}

export default ResultsVisualization
