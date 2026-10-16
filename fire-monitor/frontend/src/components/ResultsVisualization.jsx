import { PieChart, Pie, Cell, ResponsiveContainer, Tooltip, Legend } from 'recharts'
import ExportButtons from './ExportButtons'

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

  const renderBurnMappingChart = (actions) => {
    // Один сводный график: если регион был разбит на несколько ячеек
    // (несколько вызовов map_burn_area), суммы считаются по всем ячейкам.
    const list = Array.isArray(actions) ? actions : [actions]
    const summary = list.find(a => a.result_summary && !a.result_summary.error)?.result_summary
    if (!summary) return null

    const totals = {}
    let totalArea = 0
    let tilesProcessed = 0
    let tilesTotal = 0
    for (const action of list) {
      const s = action.result_summary
      if (!s || s.error) continue
      totalArea += s.total_burned_ha || 0
      tilesProcessed += s.tiles_processed || 0
      tilesTotal += s.tiles_total || 0
      for (const [key, val] of Object.entries(s.by_severity || {})) {
        if (key === 'unburned') continue
        totals[key] = (totals[key] || 0) + ((val && typeof val === 'object') ? (val.area_ha || 0) : (val || 0))
      }
    }
    if (list.length > 1) {
      summary = { ...summary, total_burned_ha: totalArea, by_severity: totals }
    }

    const severityData = Object.entries(summary.by_severity || {})
      .filter(([key]) => key !== 'unburned')
      .map(([name, data]) => ({
        name: name === 'high' ? 'Высокая' : name === 'moderate' ? 'Средняя' : 'Низкая',
        value: (data && typeof data === 'object') ? (data.area_ha || 0) : (data || 0),
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
          <div>Общая площадь: <strong>{summary.total_burned_ha?.toFixed(2) || 0} га</strong></div>
          <div>Pre-fire: <strong>{summary.pre_date || 'N/A'}</strong></div>
          <div>Post-fire: <strong>{summary.post_date || 'N/A'}</strong></div>
          {tilesTotal > 0 && (
            <div>Покрытие: <strong>{tilesProcessed}/{tilesTotal} тайлов</strong></div>
          )}
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

  // Если есть аналитический отчёт — он уже содержит итоги по всему региону,
  // поэтому отдельные «Карта гарей» по ячейкам не рисуем (иначе N ячеек = N дублей).
  const hasBurnReport = uniqueActions.some(a => a.tool === 'generate_burn_report')

  return (
    <div className="results-visualization">
      <div className="charts-container">
        {uniqueActions.map((action, idx) => {
          if (action.tool === 'search_fires') return renderFireSearchChart(action)
          if (action.tool === 'get_fire_statistics') return renderFireStatisticsChart(action)
          if (action.tool === 'map_burn_area') {
            if (hasBurnReport) return null
            if (idx > 0) return null // несколько ячеек — один сводный график на регион
            return renderBurnMappingChart(uniqueActions.filter(a => a.tool === 'map_burn_area'))
          }
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
