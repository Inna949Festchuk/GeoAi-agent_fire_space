const API_BASE = '/api'

export async function fetchFires(params = {}) {
  const searchParams = new URLSearchParams()
  if (params.bbox) searchParams.set('bbox', params.bbox.join(','))
  if (params.source && params.source !== 'all') searchParams.set('source', params.source)
  if (params.days) searchParams.set('date_from', getDaysAgo(params.days))
  if (params.min_confidence) searchParams.set('min_confidence', params.min_confidence)

  const res = await fetch(`${API_BASE}/fires/?${searchParams}`)
  if (!res.ok) throw new Error(`Failed to fetch fires: ${res.status}`)
  const data = await res.json()
  // API returns paginated response with results containing FeatureCollection
  return data.results || data
}

export async function fetchFiresFromFIRMS(params = {}) {
  // Fetch fresh data from NASA FIRMS API
  const res = await fetch(`${API_BASE}/fetch-fires/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      bbox: params.bbox,
      source: params.source || 'all',
      days: params.days || 1,
    }),
  })
  if (!res.ok) throw new Error(`Failed to fetch from FIRMS: ${res.status}`)
  return res.json()
}

export async function fetchBurns(params = {}) {
  const searchParams = new URLSearchParams()
  if (params.bbox) searchParams.set('bbox', params.bbox.join(','))
  if (params.severity) searchParams.set('severity', params.severity)

  const res = await fetch(`${API_BASE}/burns/?${searchParams}`)
  if (!res.ok) throw new Error(`Failed to fetch burns: ${res.status}`)
  const data = await res.json()
  // API returns paginated response with results containing FeatureCollection
  return data.results || data
}

export async function fetchStats(params = {}) {
  const searchParams = new URLSearchParams()
  if (params.bbox) searchParams.set('bbox', params.bbox.join(','))
  if (params.days) searchParams.set('date_from', getDaysAgo(params.days))

  const res = await fetch(`${API_BASE}/stats/?${searchParams}`)
  if (!res.ok) throw new Error(`Failed to fetch stats: ${res.status}`)
  return res.json()
}

export async function sendMessage(message, bbox = null, history = []) {
  const res = await fetch(`${API_BASE}/chat/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, bbox, history }),
  })
  if (!res.ok) throw new Error(`Chat failed: ${res.status}`)
  return res.json()
}

export async function startJob(jobType, params) {
  const res = await fetch(`${API_BASE}/jobs/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job_type: jobType, ...params }),
  })
  if (!res.ok) throw new Error(`Failed to start job: ${res.status}`)
  return res.json()
}

export async function mapBurns(params) {
  // Validate bbox size
  if (params.bbox) {
    const lonSpan = params.bbox[2] - params.bbox[0]
    const latSpan = params.bbox[3] - params.bbox[1]
    if (lonSpan > 30 || latSpan > 30) {
      throw new Error(`Region too large (${lonSpan.toFixed(1)}° × ${latSpan.toFixed(1)}°). Maximum size is 30° × 30°. Please select a smaller area.`)
    }
  }

  // Map burn severity using Sentinel-2 dNBR analysis
  const res = await fetch(`${API_BASE}/map-burns/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      bbox: params.bbox,
      pre_date: params.pre_date,
      post_date: params.post_date,
      max_cloud_cover: params.max_cloud_cover || 30,
    }),
  })
  if (!res.ok) {
    const error = await res.json()
    throw new Error(error.error || `Failed to map burns: ${res.status}`)
  }
  return res.json()
}

export async function generateReport(params) {
  const res = await fetch(`${API_BASE}/reports/generate/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      bbox: params.bbox,
      date_from: params.date_from,
      date_to: params.date_to,
      include_landcover: params.include_landcover ?? true,
    }),
  })
  if (!res.ok) {
    const error = await res.json()
    throw new Error(error.error || `Failed to generate report: ${res.status}`)
  }
  return res.json()
}

export async function getReport(reportId) {
  const res = await fetch(`${API_BASE}/reports/${reportId}/`)
  if (!res.ok) throw new Error(`Failed to fetch report: ${res.status}`)
  return res.json()
}

export async function listReports(limit = 10) {
  const res = await fetch(`${API_BASE}/reports/?limit=${limit}`)
  if (!res.ok) throw new Error(`Failed to list reports: ${res.status}`)
  const data = await res.json()
  return data.results || data
}

export async function exportReport(reportId, format = 'geojson') {
  const endpoint = format === 'csv' ? 'export_csv' : 'export_geojson'
  const res = await fetch(`${API_BASE}/reports/${reportId}/${endpoint}/`)
  if (!res.ok) throw new Error(`Failed to export report: ${res.status}`)

  if (format === 'csv') {
    return res.blob()
  }
  return res.json()
}

function getDaysAgo(days) {
  const d = new Date()
  d.setDate(d.getDate() - days)
  return d.toISOString().split('T')[0]
}
