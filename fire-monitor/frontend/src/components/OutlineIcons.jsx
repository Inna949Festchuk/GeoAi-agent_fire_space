// Упрощённые контурные иконки UI (stroke, viewBox 24×24) — единый вид
// для панели слоёв, заголовков и MapLibre-попапов. Цвет (#ff6b1a) и
// огненно-оранжевое свечение задаёт CSS-класс .ui-outline-icon.

export const ICON_PATHS = {
  flame: '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
  patch: '<path d="M5 8.5 12 4l7 4.5-1.5 8L9 20l-4.5-3.5z"/>',
  target: '<circle cx="12" cy="12" r="8"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>',
  route: '<circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="5" r="2.5"/><path d="M7.8 17.2C12 15 12 9 16.2 6.8"/>',
  station: '<path d="M4 11l8-7 8 7"/><path d="M6 9.5V20h12V9.5"/><path d="M12 13v5M9.5 15.5h5"/>',
  chart: '<path d="M4 20h16"/><path d="M7 20v-6M12 20V8M17 20v-9"/>',
  map: '<path d="M9 4 3 6.5v13L9 17l6 2.5 6-2.5v-13L15 6.5 9 4z"/><path d="M9 4v13M15 6.5v13"/>',
  layers: '<path d="M12 3 3 8l9 5 9-5-9-5z"/><path d="M3 12.5l9 5 9-5"/><path d="M3 17l9 5 9-5"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  satellite: '<path d="M13 7 9 3 5 7l4 4"/><path d="M17 11l4 4-4 4-4-4"/><path d="M8 12l4 4 6-6-4-4z"/><path d="M16 8l3-3"/><path d="M9 21a6 6 0 0 0-6-6"/>',
  trend: '<path d="M22 7l-8.5 8.5-5-5L2 17"/><path d="M16 7h6v6"/>',
}

// SVG-строка для MapLibre-попапов (вставляются через setHTML)
export const outlineIconSvg = (name, size = 16) =>
  `<svg class="ui-outline-icon inline" viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[name] || ''}</svg>`

const makeIcon = (name) => {
  return function Icon({ size = 20, className = 'ui-outline-icon' }) {
    return (
      <svg
        viewBox="0 0 24 24"
        width={size}
        height={size}
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={className}
        aria-hidden="true"
        dangerouslySetInnerHTML={{ __html: ICON_PATHS[name] }}
      />
    )
  }
}

export const FlameIcon = makeIcon('flame')
export const PatchIcon = makeIcon('patch')
export const TargetIcon = makeIcon('target')
export const RouteIcon = makeIcon('route')
export const StationIcon = makeIcon('station')
export const ChartIcon = makeIcon('chart')
export const MapIcon = makeIcon('map')
export const LayersIcon = makeIcon('layers')
export const ChevronIcon = makeIcon('chevron')
export const SatelliteIcon = makeIcon('satellite')
export const TrendIcon = makeIcon('trend')
