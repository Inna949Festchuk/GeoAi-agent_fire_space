import { exportReport } from '../api/client'

// Общие хелперы экспорта и блок кнопок «Выгрузить результат в: GeoJSON / CSV».
// Используется один раз — внизу панели результатов (см. ResultsVisualization).

export const downloadBlob = (blob, filename) => {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

export const exportToGeoJSON = (data, filename) => {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  downloadBlob(blob, filename)
}

const toWkt = (geometry) => {
  if (!geometry) return ''
  const type = geometry.type
  const coords = geometry.coordinates
  if (type === 'Polygon') {
    const rings = coords.map((ring) => ring.map(([x, y]) => `${x} ${y}`).join(', '))
    return `POLYGON((${rings.join('), (')}))`
  }
  if (type === 'MultiPolygon') {
    const polys = coords.map((poly) => {
      const rings = poly.map((ring) => ring.map(([x, y]) => `${x} ${y}`).join(', '))
      return `(${rings.join('), (')})`
    })
    return `MULTIPOLYGON(${polys.join(', ')})`
  }
  if (type === 'Point') {
    const [x, y] = coords
    return `POINT(${x} ${y})`
  }
  if (type === 'LineString') {
    return `LINESTRING(${coords.map(([x, y]) => `${x} ${y}`).join(', ')})`
  }
  return JSON.stringify(geometry)
}

const csvCell = (value) => {
  if (value === null || value === undefined) return ''
  const str = String(value)
  // Экранируем кавычки и оборачиваем поля с разделителями в кавычки
  if (/[",;\n]/.test(str)) return `"${str.replace(/"/g, '""')}"`
  return str
}

export const convertToCSV = (data) => {
  const features = Array.isArray(data) ? data : data?.features
  if (!features || features.length === 0) return ''

  // Определяем тип данных: пожары (точки) или гари (полигоны)
  const isPoint = features[0]?.geometry?.type === 'Point'

  let headers, rows
  if (isPoint) {
    headers = ['id', 'latitude', 'longitude', 'brightness', 'confidence', 'frp', 'source', 'detected_at']
    rows = features.map((f, idx) => {
      const props = f.properties || {}
      const coords = f.geometry?.coordinates || [0, 0]
      return [idx + 1, coords[1], coords[0], props.brightness, props.confidence, props.frp, props.source, props.detected_at]
    })
  } else {
    headers = ['id', 'severity', 'area_ha', 'dnbr_mean', 'pre_date', 'post_date', 'satellite', 'tile_id', 'geometry_wkt']
    rows = features.map((f, idx) => {
      const props = f.properties || {}
      return [
        idx + 1,
        props.severity,
        props.area_ha,
        props.dnbr_mean,
        props.pre_date,
        props.post_date,
        props.satellite,
        props.tile_id,
        toWkt(f.geometry),
      ]
    })
  }

  const lines = [headers, ...rows].map((row) => row.map(csvCell).join(','))
  return '\uFEFF' + lines.join('\r\n') // BOM для корректной кириллицы в Excel
}

export const exportToCSV = (data, filename) => {
  const csv = convertToCSV(data)
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
  downloadBlob(blob, filename)
}

// Загрузка полных данных из API (с пагинацией) для экспорта
export const loadFullData = async (dataType) => {
  const endpoint = dataType === 'burns' ? '/api/burns/' : '/api/fires/'
  try {
    const allFeatures = []
    let url = `${endpoint}?page_size=5000`
    let pages = 0
    while (url && pages < 20) {
      const response = await fetch(url)
      if (!response.ok) throw new Error(`Failed to load ${dataType} data`)
      const data = await response.json()
      if (Array.isArray(data)) {
        allFeatures.push(...data)
        break
      }
      allFeatures.push(...(data.features || []))
      url = data.next || null
      pages += 1
    }
    return { type: 'FeatureCollection', features: allFeatures }
  } catch (error) {
    console.error('Error loading full data:', error)
    return null
  }
}

// Экспорт конкретного аналитического отчёта по id (бэкенд отдаёт готовый файл)
export const handleExportReport = async (reportId, format) => {
  try {
    const data = await exportReport(reportId, format)

    if (format === 'csv') {
      downloadBlob(data, `burn_report_${reportId}.csv`)
    } else {
      exportToGeoJSON(data, `burn_report_${reportId}.geojson`)
    }
  } catch (error) {
    console.error('Export failed:', error)
    alert('Ошибка при экспорте отчёта')
  }
}

function ExportButtons({ dataType = 'fires' }) {
  const handleExportGeoJSON = async () => {
    const fullData = await loadFullData(dataType)
    if (fullData && fullData.features.length > 0) {
      exportToGeoJSON(fullData, `fire-monitor-${dataType}-${Date.now()}.geojson`)
    } else {
      alert('Нет данных для выгрузки')
    }
  }

  const handleExportCSV = async () => {
    const fullData = await loadFullData(dataType)
    if (fullData && fullData.features.length > 0) {
      exportToCSV(fullData, `fire-monitor-${dataType}-${Date.now()}.csv`)
    } else {
      alert('Нет данных для выгрузки')
    }
  }

  return (
    <div className="export-buttons">
      <div className="export-label">Выгрузить результат в:</div>
      <button className="btn btn-export btn-geojson" onClick={handleExportGeoJSON}>
        GeoJSON
      </button>
      <button className="btn btn-export btn-csv" onClick={handleExportCSV}>
        CSV
      </button>
    </div>
  )
}

export default ExportButtons
