import { useState } from 'react'
import { FlameIcon, PatchIcon, TargetIcon, RouteIcon, StationIcon, ChartIcon, LayersIcon, ChevronIcon } from './OutlineIcons'

// Контурные иконки слоёв (заменяют эмодзи; свечение — класс .ui-outline-icon)
const LAYER_ICONS = {
  fires: FlameIcon,
  burns: PatchIcon,
  validation: TargetIcon,
  routes: RouteIcon,
  fire_stations: StationIcon,
  custom: ChartIcon,
}

// Конфигурация слоёв
const LAYERS_CONFIG = [
  {
    id: 'fires',
    name: 'Очаги пожаров',
    color: '#e94560',
    description: 'Термальные аномалии со спутников',
    legend: [
      { color: '#e94560', label: 'Высокая уверенность' },
      { color: '#ff8c42', label: 'Номинальная' },
      { color: '#4ade80', label: 'Низкая' },
    ],
  },
  {
    id: 'burns',
    name: 'Гари',
    color: '#dc2626',
    description: 'Выгоревшие территории (Sentinel-2)',
    legend: [
      { color: '#dc2626', label: 'Высокая степень' },
      { color: '#ea580c', label: 'Средняя' },
      { color: '#ca8a04', label: 'Низкая' },
      { color: '#65a30d', label: 'Не затронуто' },
    ],
  },
  {
    id: 'validation',
    name: 'Валидация',
    color: '#3b82f6',
    description: 'Стрела vs Рослесхоз / MCD64A1',
    legend: [
      { color: '#3b82f6', label: 'Рослесхоз (GT)' },
      { color: '#ef4444', label: 'Детекции Стрела' },
      { color: '#ea580c', label: 'Гари Стрела', type: 'polygon' },
      { color: '#16a34a', label: 'NASA MCD64A1 (эталон)', type: 'polygon' },
      { color: '#9ca3af', label: 'Промзоны', type: 'polygon' },
    ],
  },
  {
    id: 'routes',
    name: 'Маршруты',
    color: '#ff6b35',
    description: 'Маршруты по дорогам (OSRM)',
    legend: [
      { color: '#ff6b35', label: 'Маршрут', type: 'line' },
      { color: '#4ade80', label: 'Начало', type: 'point' },
      { color: '#e94560', label: 'Конец', type: 'point' },
    ],
  },
  {
    id: 'fire_stations',
    name: 'Пожарные части',
    color: '#00d4ff',
    description: 'Пожарно-спасательные подразделения',
    legend: [
      { color: '#00d4ff', label: 'Пожарная часть', type: 'point' },
    ],
  },
  {
    id: 'custom',
    name: 'Результаты анализа',
    color: '#9d4edd',
    description: 'Данные из Python sandbox',
    legend: [
      { color: '#9d4edd', label: 'Полигоны', type: 'polygon' },
      { color: '#00d4ff', label: 'Точки', type: 'point' },
    ],
  },
]

// iOS-style Toggle компонент.
// Активный цвет у всех переключателей единый — огненно-оранжевый (CSS .ios-toggle-slider).
function Toggle({ checked, onChange, disabled = false, row = false, children = null }) {
  return (
    <label className={`ios-toggle${row ? ' ios-toggle-row' : ''}${disabled ? ' ios-toggle-disabled' : ''}`}>
      {children}
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        disabled={disabled}
      />
      <span className="ios-toggle-slider"></span>
    </label>
  )
}

// Компонент слоя с легендой
function LayerItem({ layer, enabled, onToggle, hasData, validationSublayers }) {
  const [showLegend, setShowLegend] = useState(false)
  const Icon = LAYER_ICONS[layer.id]

  return (
    <div className={`layer-item ${!hasData ? 'layer-item-disabled' : ''}`}>
      <div className="layer-item-header">
        <div className="layer-item-info">
          <span className="layer-item-icon"><Icon /></span>
          <div className="layer-item-text">
            <span className="layer-item-name">{layer.name}</span>
            <span className="layer-item-desc">{layer.description}</span>
          </div>
        </div>
        <div className="layer-item-controls">
          {hasData && layer.legend && (
            <button
              className="layer-legend-btn"
              onClick={() => setShowLegend(!showLegend)}
              title={showLegend ? 'Скрыть легенду' : 'Показать легенду'}
            >
              <ChevronIcon size={14} className="toggle-chevron" />
            </button>
          )}
          <Toggle
            checked={enabled && hasData}
            onChange={onToggle}
          />
        </div>
      </div>

      {/* Подслои валидации — iOS-переключатели с огненно-оранжевым активным состоянием */}
      {layer.id === 'validation' && hasData && validationSublayers && showLegend && (
        <div className="validation-sublayers">
          {validationSublayers.map((sub) => (
            <Toggle
              key={sub.id}
              row
              checked={sub.checked}
              onChange={sub.onChange}
              disabled={!enabled}
            >
              <span
                className="validation-sublayer-color"
                style={{ backgroundColor: sub.color }}
              ></span>
              <span className="validation-sublayer-label">{sub.label}</span>
            </Toggle>
          ))}
        </div>
      )}

      {showLegend && hasData && layer.legend && layer.id !== 'validation' && (
        <div className="layer-legend">
          {layer.legend.map((item, idx) => (
            <div key={idx} className="legend-row">
              {item.type === 'line' ? (
                <span className="legend-line" style={{ backgroundColor: item.color }}></span>
              ) : item.type === 'polygon' ? (
                <span className="legend-polygon" style={{ backgroundColor: item.color }}></span>
              ) : (
                <span className="legend-dot" style={{ backgroundColor: item.color }}></span>
              )}
              <span className="legend-label">{item.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// Главная панель слоёв
function LayerPanel({
  showFires, setShowFires,
  showBurns, setShowBurns,
  showRoutes, setShowRoutes,
  showFireStations, setShowFireStations,
  showCustom, setShowCustom,
  showValidation, setShowValidation,
  showValGt, setShowValGt,
  showValStrela, setShowValStrela,
  showValBurn, setShowValBurn,
  showValModis, setShowValModis,
  showValZone, setShowValZone,
  hasFires, hasBurns, hasRoutes, hasFireStations, hasCustom, hasValidation
}) {
  const [isCollapsed, setIsCollapsed] = useState(false)

  const layerStates = {
    fires: { enabled: showFires, setter: setShowFires, hasData: hasFires },
    burns: { enabled: showBurns, setter: setShowBurns, hasData: hasBurns },
    validation: { enabled: showValidation, setter: setShowValidation, hasData: hasValidation },
    routes: { enabled: showRoutes, setter: setShowRoutes, hasData: hasRoutes },
    fire_stations: { enabled: showFireStations, setter: setShowFireStations, hasData: hasFireStations },
    custom: { enabled: showCustom, setter: setShowCustom, hasData: hasCustom },
  }

  // Подслои валидации для чекбоксов
  const validationSublayers = [
    { id: 'gt', label: 'Рослесхоз (GT)', color: '#3b82f6', checked: showValGt, onChange: setShowValGt },
    { id: 'strela', label: 'Детекции Стрела', color: '#ef4444', checked: showValStrela, onChange: setShowValStrela },
    { id: 'burn', label: 'Гари Стрела', color: '#ea580c', checked: showValBurn, onChange: setShowValBurn },
    { id: 'modis', label: 'NASA MCD64A1 (эталон)', color: '#16a34a', checked: showValModis, onChange: setShowValModis },
    { id: 'zone', label: 'Промзоны', color: '#9ca3af', checked: showValZone, onChange: setShowValZone },
  ]

  const activeLayersCount = Object.values(layerStates).filter(s => s.enabled && s.hasData).length

  return (
    <div className={`layer-panel ${isCollapsed ? 'layer-panel-collapsed' : ''}`}>
      <div className="layer-panel-header">
        <h3><LayersIcon size={16} /> Слои карты</h3>
        <div className="layer-panel-header-controls">
          <span className="layer-count">{activeLayersCount} активных</span>
          <button
            className="layer-collapse-btn"
            onClick={() => setIsCollapsed(!isCollapsed)}
            title={isCollapsed ? 'Развернуть' : 'Свернуть'}
          >
            <ChevronIcon size={14} className="toggle-chevron" />
          </button>
        </div>
      </div>

      {!isCollapsed && (
        <div className="layer-panel-content">
          {LAYERS_CONFIG.map(layer => {
            const state = layerStates[layer.id]
            return (
              <LayerItem
                key={layer.id}
                layer={layer}
                enabled={state.enabled}
                onToggle={state.setter}
                hasData={state.hasData}
                validationSublayers={layer.id === 'validation' ? validationSublayers : null}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}

export default LayerPanel
export { LAYERS_CONFIG }
