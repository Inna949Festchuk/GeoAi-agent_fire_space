# Fire Monitor — QWEN.md

## Project Overview

**Fire Monitor** — это веб-приложение для мониторинга лесных пожаров в режиме реального времени. Система получает данные о термальных аномалиях со спутников NASA FIRMS (VIIRS/MODIS), фильтрует ложные срабатывания (промышленные объекты, солнечные блики, вулканы), оценивает степень выгорания лесов по методу dNBR на основе Sentinel-2, и предоставляет интерактивную карту с AI-чатом для анализа ситуации.

### Архитектура

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Frontend   │────▶│   Nginx     │◀────│   Backend   │
│  React+Vite │     │  (reverse   │     │   Django    │
│  MapLibre   │     │   proxy)    │     │  + PostGIS  │
│  :5173      │     │  :80        │     │  :8000      │
└─────────────┘     └──────┬──────┘     └──────┬──────┘
                           │                    │
                           │             ┌──────┴──────┐
                           │             │  MCP Server │
                           └────────────▶│  (SSE/AI)   │
                                         │  :8001      │
                                         └─────────────┘
```

### Стек технологий

| Компонент | Технологии |
|-----------|-----------|
| **Backend** | Django 5.1, DRF, PostGIS, Gunicorn |
| **Frontend** | React 18, Vite 6, MapLibre GL JS, Recharts |
| **Database** | PostgreSQL + PostGIS |
| **Геообработка** | rasterio, geopandas, shapely, pystac-client, planetary-computer |
| **AI/LLM** | OpenAI-совместимый API (настраивается через переменные окружения) |
| **MCP Server** | Python, SSE-транспорт для интеграции с AI-агентами |
| **Инфраструктура** | Docker, Docker Compose, Nginx |

## Building & Running

### Переменные окружения

Скопируйте `.env.example` в `.env` и заполните:

```bash
cp .env.example .env
```

Ключевые переменные:
- `FIRMS_API_KEY` — ключ NASA FIRMS API ([получить здесь](https://firms.modaps.eosdis.nasa.gov/api/))
- `LLM_API_KEY`, `LLM_API_BASE_URL`, `LLM_MODEL` — настройки LLM для AI-чата
- `DJANGO_SECRET_KEY` — секретный ключ Django

### Development (hot-reload, без nginx)

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

- Frontend: http://localhost:5173
- Backend API: http://localhost:8000/api/
- MCP Server: http://localhost:8001
- Django Admin: http://localhost:8000/admin/

Код смонтирован через volumes — изменения в Python/JS подхватываются автоматически.

### Production (с nginx)

```bash
docker compose up -d --build
```

- Всё через Nginx на порту 80: http://localhost/

### Пересборка после изменений

После изменений в коде **нужно пересобирать Docker образ**, а не просто перезапускать контейнер:

```bash
# Dev
docker compose -f docker-compose.dev.yml up -d --build <service>

# Prod
docker compose up -d --build
```

Команда `docker compose restart` НЕ копирует изменённые файлы в контейнер.

### Полезные команды

```bash
# Логи
docker compose -f docker-compose.dev.yml logs -f backend

# Django shell
docker compose -f docker-compose.dev.yml exec backend python manage.py shell

# Миграции (выполняются автоматически при старте в dev)
docker compose -f docker-compose.dev.yml exec backend python manage.py migrate

# Создание миграций
docker compose -f docker-compose.dev.yml exec backend python manage.py makemigrations fires

# Тесты
docker compose -f docker-compose.dev.yml exec backend python manage.py test fires
```

## Project Structure

```
fire-monitor/
├── backend/
│   ├── config/              # Django настройки (settings.py, urls.py, wsgi.py)
│   ├── fires/               # Основное Django-приложение
│   │   ├── models.py        # FireHotspot, BurnArea, ProcessingJob
│   │   ├── views.py         # API endpoints
│   │   ├── serializers.py   # DRF сериализаторы
│   │   ├── urls.py          # Маршруты API
│   │   └── management/      # Django management commands
│   ├── geo_processing/      # Геообработка
│   │   ├── firms_client.py  # Клиент NASA FIRMS API
│   │   ├── fire_filter.py   # Фильтрация ложных срабатываний
│   │   ├── burn_severity.py # Расчёт dNBR по Sentinel-2
│   │   ├── stac_client.py   # Клиент STAC (Planetary Computer)
│   │   ├── validation/      # Валидация run_validation: GT, дедупликация, метрики
│   │   └── chat_handler.py  # Обработка AI-чата
│   ├── manage.py
│   └── requirements.txt
├── frontend/
│   ├── src/                 # React-приложение
│   ├── index.html
│   ├── package.json
│   └── vite.config.js
├── mcp_server/
│   ├── server.py            # MCP-сервер для AI-интеграции (10 tools, включая run_validation)
│   └── requirements.txt
├── db/
│   ├── Dockerfile           # PostGIS образ
│   └── init/                # Инициализация БД
├── nginx/
│   └── nginx.conf           # Reverse proxy конфигурация
├── docker-compose.yml       # Production
├── docker-compose.dev.yml   # Development
├── .env.example             # Шаблон переменных окружения
├── validation/              # Валидация: VALIDREADME.md + downnasagov.py (MCD64A1)
├── skills/                  # Промты и документация скиллов (валидация)
├── data/                    # Данные: ground_truth/, mcd64a1/, strela/
└── Seince.md                # Научное обоснование алгоритмов
```

## Key Domain Concepts

### Модели данных

- **FireHotspot** — термальная точка (VIIRS/MODIS): координаты, температура, confidence (low/nominal/high), FRP (Fire Radiative Power), источник
- **BurnArea** — полигон выгорания (Sentinel-2 dNBR): геометрия, площадь, severity (unburned/low/moderate/high)
- **ProcessingJob** — фоновая задача обработки (fire_detection / burn_mapping)

### Фильтрация пожаров

Система отсеивает ложные срабатывания:
- Промышленные объекты (нефтяные факелы) — по региону и FRP
- Солнечные блики — по спектральному соотношению
- Вулканы — по FIRMS type=3
- Сельхозпалы — по контексту

### Burn Severity (dNBR)

Расчёт степени выгорания по Sentinel-2:
- NBR = (NIR - SWIR2) / (NIR + SWIR2)
- dNBR = NBR_pre - NBR_post
- Классификация: unburned (<100), low (100-269), moderate (270-439), high (≥440)

### Валидация (run_validation, 2026-10-20 → 23)

Количественная валидация «Стрелы» по независимым источникам:
- **Детекция:** Рослесхоз 1T-ИСДМ (GT) vs детекции VIIRS/MODIS — P/R/F1,
  геодезический буфер 500 м; **дедупликация** сырых детекций в уникальные
  очаги (≤500 м + ≤±3 дня, `VAL_DEDUPE=1`); сопоставление **по периоду
  активности очага** `[first_at, last_at]` ±3 дня; жадная 1:1-привязка.
- **Гари:** Sentinel-2 dNBR vs NASA MCD64A1 (коллекция 6.1) — IoU,
  корреляция площадей (Пирсон), burn F1, отклонение, лаг; ±20 дней,
  буфер 1 км; площади/буферы — в EPSG:3576.
- **Точки входа:** REST `POST /api/validation/run/` (+ `/prepare/`,
  `GET /status/`), чат-тул `run_validation` (сводка + слой «Валидация»
  на карте), MCP-тул `run_validation`.
- **Данные:** `data/ground_truth/` (Рослесхоз), `data/mcd64a1/` (растры),
  `data/strela/` (`viirs_detections.geojson` — из FIRMS Archive для истории;
  либо БД FireHotspot/BurnArea, `source=auto|db|files`).
- **Артефакты:** `reports/validation_report.md`, `validation_map.html`,
  `metrics_summary.csv`.
- **Эко-параметры:** `VAL_*` в `.env` (буферы, окна, `VAL_DEDUPE*`).
- **Тесты:** `backend/fires/tests_validation.py` — 16 оффлайн-тестов
  (`manage.py test fires.tests_validation`).

## Recent Improvements (Current Session)

### Исправления критических проблем

1. **Проекция координат гарей**
   - Проблема: Полигоны гарей создавались в UTM проекции, а карта ожидала WGS84
   - Решение: Добавлена автоматическая репроекция через `pyproj.Transformer`
   - Файлы: `views.py`, `chat_handler.py`, `map_burns.py`

2. **Лимит bbox для burn mapping**
   - Увеличен с 3° до 10° (покрывает Иркутскую область ~775 тыс. км²)
   - Добавлена валидация на backend (`views.py`, `burn_severity.py`)
   - Добавлена валидация на frontend (`client.js`)
   - Обновлён AI промпт для работы с новым лимитом

3. **Предотвращение OOM при обработке больших bbox**
   - Добавлен автоматический downsampling растров до 3000×3000 пикселей
   - Используется `rasterio.windows.Window` для чтения только нужной области
   - Ресемплинг SWIR2 (20м) к NIR (10м) через `numpy.repeat`

### Новые возможности

4. **Система экспорта данных**
   - Экспорт в GeoJSON и CSV через кнопки в чате
   - Умная логика: определяет тип данных (fires/burns) на основе контекста
   - Загрузка полных данных из API с пагинацией (`page_size=10000`)
   - Файлы: `ResultsVisualization.jsx`, `client.js`

5. **Визуализация результатов**
   - Круговые диаграммы для поиска пожаров (подтверждено/отфильтровано)
   - Две мини-диаграммы для статистики (по источникам и уверенности)
   - Диаграмма для карты гарей (по степени тяжести)
   - Используется библиотека `recharts`

6. **Улучшенный AI чат**
   - Возвращает `map_data_list` (массив) вместо `map_data` (один объект)
   - Поддержка множественных результатов (пожары + гари одновременно)
   - Markdown рендеринг ответов через `react-markdown`
   - Автоматическое центрирование карты на первой гари (плавная анимация 1.5 сек)
   - Форматирование действий в виде читаемых таблиц вместо сырого JSON

7. **Кастомная пагинация API**
   - Создан `FlexiblePagination` class с параметром `page_size`
   - Позволяет загружать до 10000 записей за один запрос
   - Файл: `fires/pagination.py`

### Технические детали

**Репроекция координат:**
```python
from pyproj import Transformer
from shapely.ops import transform as shapely_transform

transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
project = lambda x, y: transformer.transform(x, y)
merged_wgs84 = shapely_transform(project, merged)
```

**Downsampling для экономии памяти:**
```python
if target_width > max_dimension or target_height > max_dimension:
    downsample = max(target_width // max_dimension, target_height // max_dimension)
    out_shape = (target_height // downsample, target_width // downsample)
    nir = src_nir.read(1, window=window_nir, out_shape=out_shape, 
                       resampling=rasterio.enums.Resampling.average)
```

**Умный экспорт:**
```javascript
const getExportType = () => {
  const hasBurns = actions.some(action => action.tool === 'map_burn_area')
  const hasFires = actions.some(action => 
    action.tool === 'search_fires' || action.tool === 'get_fire_statistics'
  )
  
  if (hasBurns && !hasFires) return 'burns'
  if (hasFires && !hasBurns) return 'fires'
  return 'both'
}
```

### Производительность

| Операция | Время | Результат |
|----------|-------|-----------|
| Burn mapping bbox 3°×3° | ~35 сек | 3 полигона |
| Burn mapping bbox 10°×10° | ~50-60 сек | 3 полигона |
| Загрузка 2004 пожаров | <1 сек | Все записи |
| AI чат (только пожары) | ~25 сек | 2004 точки |
| AI чат (пожары + гари) | ~120 сек | 2004 точки + 3 полигона |

### Известные ограничения

- Максимальный bbox для burn mapping: 10° × 10° (~1000 км × 1000 км)
- Для больших регионов нужно разбивать на несколько запросов
- AI иногда пытается использовать bbox > 10° для больших регионов (улучшается через промпт)
- Кнопки экспорта могут не отображаться если `actions` пустой (нужна проверка)

## Development Conventions

- **Язык кода:** Python (backend), JavaScript/JSX (frontend)
- **Язык документации и комментариев:** русский (в `Seince.md`, комментариях), английский (в коде — имена переменных, docstrings)
- **API:** REST через Django REST Framework, документация через drf-spectacular
- **ГИС:** все геометрии в SRID 4326 (WGS84), используется PostGIS
- **Фронтенд:** функциональные компоненты React, без TypeScript
- **Docker:** все сервисы контейнеризированы, dev/prod разделены через разные compose-файлы
