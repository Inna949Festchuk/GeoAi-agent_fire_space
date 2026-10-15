import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip, Legend } from 'recharts'
import ExportButtons, { handleExportReport } from './ExportButtons'

// Возвращает «ключ» действия: одинаковые вызовы одного инструмента
// (например, agent дважды дёрнул search_fires с теми же параметрами)
// считаем одним действием, чтобы не рисовать дублирующиеся графики.
const actionKey = (action) => {
  const args = typeof action.args === 'string' ? action.args : JSON.stringify(action.args || {})
  return `${action.tool}:${args}`
}

const dedupeActions = (actions) => {
  const seen = new Set()
  const unique = []
  for (const action of actions) {
    const key = actionKey(action)
    if (seen.has(key)) continue
    seen.add(key)
    unique.push(action)
  }
  return unique
}

function ResultsVisualization({ actions }) {
  if (!actions || actions.length === 0) return null

  // Убираем дубликаты действий — один график на уникальное действие
  const uniqueActions = dedupeActions(actions)

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
              <Tooltip formatter={(value) => `${Number(value).toFixed(2)} га`} />
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

        {/* Экспорт конкретного отчёта выполняется через API-эндпоинты reports/{id}/export_* */}
        {reportId && (
          <div className="report-actions">
            <button
              className="btn btn-small btn-export btn-geojson"
              onClick={() => handleExportReport(reportId, 'geojson')}
            >
              GeoJSON отчёта
            </button>
            <button
              className="btn btn-small btn-export btn-csv"
              onClick={() => handleExportReport(reportId, 'csv')}
            >
              CSV отчёта
            </button>
          </div>
        )}
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

  // Тип данных для общего блока экспорта: гари или пожары
  const hasBurns = uniqueActions.some(a => a.tool === 'map_burn_area' || a.tool === 'generate_burn_report')
  const dataType = hasBurns ? 'burns' : 'fires'

  return (
    <div className="results-visualization">
      <div className="charts-container">
        {uniqueActions.map((action, idx) => {
          if (action.tool === 'search_fires') return renderFireSearchChart(action)
          if (action.tool === 'get_fire_statistics') return renderFireStatisticsChart(action)
          if (action.tool === 'map_burn_area') return renderBurnMappingChart(action)
          if (action.tool === 'generate_burn_report') return renderBurnReportChart(action)
          if (action.tool === 'search_sentinel2_scenes') return renderSentinel2Chart(action)
          return null
        })}
      </div>

      {/* Единый блок кнопок выгрузки — рендерится один раз на всю панель результатов */}
      <ExportButtons dataType={dataType} />
    </div>
  )
}

export default ResultsVisualization
