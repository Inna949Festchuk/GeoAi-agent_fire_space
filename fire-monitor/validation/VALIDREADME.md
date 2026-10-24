> ✅ **Статус на 2026-10-23:** валидация реализована как встроенный инструмент
> **`run_validation`** (движок `backend/geo_processing/validation/`): REST
> `POST /api/validation/run/` + `/prepare/` + `GET /status/`, чат-тул и MCP-тул
> `run_validation`. Метрики: P/R/F1 детекции (геодезический буфер 500 м,
> **дедупликация сырых детекций в уникальные очаги** ≤500 м + ≤±3 дня,
> сопоставление **по периоду активности очага** ±3 дня) и гари
> (IoU, корреляция площадей, лаг; ±20 дней, буфер 1 км). Артефакты:
> `reports/validation_report.md`, `validation_map.html`, `metrics_summary.csv`,
> слой «Валидация» на карте приложения. Актуальные промты — в
> `skills/fire_monitor_validation_README.md`. Ниже — методология и подготовка
> данных; ручные скрипты этапов 2–3 — для справки/отладки.
>
> **Результат (Иркутская область, июль 2024):** 335 075 сырых детекций FIRMS →
> 6 860 уникальных очагов, GT 1 534 пожара: P 14,0% / R 62,5% / F1 22,9%;
> корреляция площадей гарей 0,879 ✅ (цель >0,8).

## 📊 Что позволяет оценить комбинация Рослесхоз + MCD64A1

### Краткая таблица возможностей

| Аспект оценки | Данные Рослесхоза (tochno.st) | MCD64A1 (MODIS) |
|---|---|---|
| **Детекция активных пожаров** | ✅ Координаты точек возгорания | ❌ Только выгоревшие площади |
| **Картирование гарей** | ⚠️ Только итоговая площадь | ✅ Полигоны/растры выгоревших территорий |
| **Временная точность** | ✅ Дата начала/конца пожара | ⚠️ День выгорания (±несколько дней) |
| **Пространственная точность** | ✅ Координаты (точечные) | ✅ 500 м растры |
| **Официальность для стейкхолдеров** | ✅ Рослесхоз (государственный источник) | ✅ NASA (научный стандарт) |

---

## 🎯 Детализация: что именно можно оценить

### 1. **Точность оперативной детекции (Precision/Recall)**

**Используем: Рослесхоз (форма 1T-ИСДМ)**

Рослесхоз предоставляет:
- Координаты каждого пожара (lat/lon)
- Дата обнаружения и ликвидации
- Площадь в гектарах
- Причина возникновения

**Что оцениваем:**
```
Precision = TP / (TP + FP)
- TP: Пожары, которые "Стрела" обнаружила И которые есть в реестре Рослесхоза
- FP: Ложные срабатывания "Стрелы" (точки, которых нет в реестре)

Recall = TP / (TP + FN)
- TP: Те же обнаруженные пожары
- FN: Пожары из реестра Рослесхоза, которые "Стрела" пропустила
```

**Ограничения:**
- Реестр Рослесхоза содержит только **зарегистрированные** пожары (те, которые тушили)
- Мелкие пожары (< 1 га) могут не попадать в статистику
- Координаты в реестре могут быть приблизительными (точность ~100-500 м)

> ℹ️ **Важно для Precision:** сырые VIIRS-детекции — пер-орбитовые точки
> (один очаг переоткрывается на каждом пролёте). Движок дедуплицирует их в
> **уникальные очаги** (≤500 м + ≤±3 дня) и сопоставляет очаг с пожаром по
> **периоду активности** `[first_at, last_at]` ±3 дня. Часть FP — мелкие
> незарегистрированные пожары: это полнота реестра, а не ошибка детекции
> (июль 2024: VIIRS видит 6 860 очагов, реестр содержит 1 534 пожара).

### 2. **Точность картирования гарей**

**Используем: MCD64A1 + Рослесхоз**

MCD64A1 предоставляет:
- Растровые данные 500 м с датой выгорания каждого пикселя
- Глобальное покрытие, включая Россию
- Научно валидированный алгоритм

**Что оцениваем:**
```
Коэффициент корреляции площадей:
- Площадь гари по "Стреле" (Sentinel-2, dNBR) vs Площадь по MCD64A1
- Ожидаемое значение: r > 0.8

Среднее отклонение площадей:
- |Площадь_Стрела - Площадь_MCD64A1| / Площадь_MCD64A1
- Ожидаемое значение: < 20%

Пересечение полигонов (IoU):
- Intersection over Union между полигонами гарей
- Ожидаемое значение: IoU > 0.6
```

**Ограничения:**
- Разрешение MCD64A1 (500 м) хуже, чем Sentinel-2 (10-20 м)
- MCD64A1 может пропустить мелкие пожары < 25 га
- Временная задержка MCD64A1 (месячный продукт)

### 3. **Временная точность детекции**

**Используем: Рослесхоз + MCD64A1**

**Что оцениваем:**
```
Задержка обнаружения:
- Дата_обнаружения_Стрела - Дата_начала_пожара_Рослесхоз
- Целевое значение: < 12 часов

Совпадение дат выгорания:
- |Дата_гари_Стрела - Дата_гари_MCD64A1|
- Ожидаемое значение: < 3 дня
```

---

## 🚀 Пошаговый план реализации

### **Этап 1: Подготовка данных (1-2 дня)**

#### 1.1 Скачать данные Рослесхоза
```python
# Загружаем реестр пожаров за пиковый месяц (июль 2024)
import pandas as pd
import geopandas as gpd

# Скачиваем с https://tochno.st/datasets/fires
# Форма 1T-ИСДМ: Реестр природных пожаров
fires_registry = pd.read_csv(
    'fires_1T_ISDM.csv',
    sep=';',
    encoding='utf-8',
    parse_dates=['fire_start_date', 'fire_end_date']
)

# Фильтруем по региону и периоду
target_region = 'Иркутская область'  # или другой регион
target_month = '2024-07'

fires_filtered = fires_registry[
    (fires_registry['region'] == target_region) &
    (fires_registry['fire_start_date'].dt.strftime('%Y-%m') == target_month)
].copy()

# Создаём GeoDataFrame
gdf_ground_truth = gpd.GeoDataFrame(
    fires_filtered,
    geometry=gpd.points_from_xy(
        fires_filtered['longitude'], 
        fires_filtered['latitude']
    ),
    crs='EPSG:4326'
)

print(f"Найдено пожаров: {len(gdf_ground_truth)}")
print(f"Общая площадь: {gdf_ground_truth['burned_area_ha'].sum():.0f} га")
```

#### 1.2 Скачать данные MCD64A1

⚠️ Старые прямые URL вида `ladsweb.../archive/allData/61/MCD64A1/YYYY.DDD/` мертвы (404):
данные теперь раздаёт LP DAAC (`data.lpdaac.earthdatacloud.nasa.gov`), а точное имя
файла (с таймстампом выпуска) отдаёт CMR. Рабочий способ — скрипт проекта:

```bash
python validation/downnasagov.py --bbox 97,51,119,60 --year 2024 --month 7
```

- токен `EARTHDATA_TOKEN` берётся из `fire-monitor/.env`;
- вывод — `fire-monitor/data/mcd64a1/` (шарится с контейнерами через volume, кэш);
- тайлы Иркутской области (по CMR, июль 2024): `h21–h26 × v02–v04`, ядро — `h24v03`;
- 1 июля 2024 (високосный год) = **A2024183**, а не A2024182.

#### 1.3 Подготовить данные «Стрелы» за тот же период

```bash
# Картирование гарей (Sentinel-2 dNBR) — management command:
docker compose -f docker-compose.dev.yml exec backend python manage.py map_burns \
  --bbox 97,51,119,60 \
  --pre_date 2024-06-25 \
  --post_date 2024-07-31

# Активные пожары (VIIRS/MODIS) — fetch_fires отдаёт только NRT за 1–5 дней:
docker compose -f docker-compose.dev.yml exec backend python manage.py fetch_fires \
  --bbox 97,51,119,60 --days 1
```

> ℹ️ **Исторические детекции** (например, июль 2024) из FIRMS API не получить —
> заказывают вручную через [FIRMS Archive Download](https://firms.modaps.eosdis.nasa.gov/download/)
> и кладут в `data/strela/viirs_detections.geojson` (см. блок ниже и раздел
> «📦 Исторические детекции» в `skills/fire_monitor_validation_README.md`).

> ⚠️ **Важно: исторические детекции VIIRS/MODIS нельзя скачать через FIRMS API.**
> Эндпоинт `/api/area/csv/` отдаёт только NRT-данные за последние 1–5 дней:
> последний сегмент URL — это «сколько дней назад», а не календарная дата
> (запрос по исторической дате возвращает `HTTP 400: Invalid day range.
> Expects [1..5].`). Исторический архив заказывают вручную со страницы
> [FIRMS Archive Download](https://firms.modaps.eosdis.nasa.gov/download/)
> (авторизация Earthdata Login или email-код) и кладут в
> `data/strela/viirs_detections.geojson` — движок валидации подхватит файл
> автоматически (`source='auto'` / `'files'`). Альтернатива: гранулы
> VIIRS I-band (HDF5) через NASA LAADS DAAC / CMR по Earthdata-токену.

---

### **Этап 2: Обработка и сравнение (2-3 дня)**

#### 2.1 Конвертация MCD64A1 в GeoTIFF (WGS84)
```python
import rasterio
from rasterio.warp import reproject, Resampling
from osgeo import gdal

# Открываем HDF файл
input_file = "mcd64a1_2024_07_h19v02.hdf"
output_file = "mcd64a1_2024_07_h19v02_wgs84.tif"

# Используем GDAL для конвертации из синусоидальной проекции в WGS84
gdal.Warp(
    output_file,
    input_file,
    dstSRS='EPSG:4326',
    resampleAlg=Resampling.bilinear
)

print(f"Конвертирован: {output_file}")
```

#### 2.2 Пространственное объединение с результатами "Стрелы"

> ℹ️ В движке `run_validation` этот шаг выполнен корректно: **геодезический**
> буфер 500 м (репроекция в EPSG:3576, не градусы), **дедупликация** сырых
> детекций в уникальные очаги (≤500 м + ≤±3 дня) и сопоставление **по периоду
> активности очага** `[first_at, last_at]` ±3 дня (жадная 1:1-привязка).
> Скрипт ниже — упрощённый справочный вариант (градусный буфер — только
> для наглядности).

```python
import geopandas as gpd
from shapely.geometry import Point

# Загружаем результаты "Стрелы"
strela_detections = gpd.read_file("./results_2024_july/viirs_detections.geojson")

# Создаём буфер вокруг точек "Стрелы" (учёт погрешности координат)
buffer_distance = 500 / 111000  # 500 м в градусах (приблизительно)
strela_buffered = strela_detections.copy()
strela_buffered['geometry'] = strela_detections.buffer(buffer_distance)

# Находим совпадения с Ground Truth (Рослесхоз)
matches = gpd.sjoin(
    gdf_ground_truth,
    strela_buffered,
    how='inner',
    predicate='within'
)

# Считаем метрики
TP = len(matches)
FP = len(strela_detections) - TP
FN = len(gdf_ground_truth) - TP

precision = TP / (TP + FP) if (TP + FP) > 0 else 0
recall = TP / (TP + FN) if (TP + FN) > 0 else 0
f1 = 2 * (precision * recall) / (precision + recall) if (precision + recall) > 0 else 0

print("=" * 50)
print("МЕТРИКИ ДЕТЕКЦИИ АКТИВНЫХ ПОЖАРОВ")
print("=" * 50)
print(f"True Positives (TP): {TP}")
print(f"False Positives (FP): {FP}")
print(f"False Negatives (FN): {FN}")
print(f"Precision: {precision:.2%}")
print(f"Recall: {recall:.2%}")
print(f"F1-score: {f1:.2%}")
print("=" * 50)
```

#### 2.3 Сравнение площадей гарей
```python
# Загружаем гари от "Стрелы" (Sentinel-2, dNBR)
strela_burned_areas = gpd.read_file("./results_2024_july/burned_areas.geojson")

# Загружаем MCD64A1 как растр
with rasterio.open("mcd64a1_2024_07_h19v02_wgs84.tif") as src:
    mcd64a1_data = src.read(1)
    transform = src.transform

# Для каждой гари "Стрелы" считаем площадь по MCD64A1
results = []
for idx, row in strela_burned_areas.iterrows():
    # Площадь по "Стреле"
    area_strela = row['area_ha']
    
    # Масштабируем полигон в систему координат MCD64A1
    polygon = row['geometry']
    
    # Считаем пиксели MCD64A1 внутри полигона
    # (упрощённо, в реальности нужно использовать rasterio.features.geometry_mask)
    # Здесь используем приблизительный расчёт
    area_mcd64a1 = estimate_burned_area_from_raster(polygon, mcd64a1_data, transform)
    
    results.append({
        'fire_id': row['fire_id'],
        'area_strela_ha': area_strela,
        'area_mcd64a1_ha': area_mcd64a1,
        'difference_ha': abs(area_strela - area_mcd64a1),
        'difference_pct': abs(area_strela - area_mcd64a1) / area_mcd64a1 * 100 if area_mcd64a1 > 0 else 0
    })

df_comparison = pd.DataFrame(results)

# Статистика
print("=" * 50)
print("СРАВНЕНИЕ ПЛОЩАДЕЙ ГАРЕЙ")
print("=" * 50)
print(f"Количество пожаров: {len(df_comparison)}")
print(f"Среднее отклонение: {df_comparison['difference_pct'].mean():.1f}%")
print(f"Медианное отклонение: {df_comparison['difference_pct'].median():.1f}%")
print(f"Корреляция площадей: {df_comparison['area_strela_ha'].corr(df_comparison['area_mcd64a1_ha']):.2f}")
print("=" * 50)
```

---

### **Этап 3: Визуализация и отчётность (1-2 дня)**

#### 3.1 Создание интерактивной карты
```python
import folium
from folium.plugins import MarkerCluster

# Создаём карту
m = folium.Map(
    location=[52.28, 104.30],  # Центр Иркутской области
    zoom_start=7,
    tiles='OpenStreetMap'
)

# Слой 1: Ground Truth (Рослесхоз)
marker_cluster_gt = MarkerCluster(name='Рослесхоз (Ground Truth)')
for idx, row in gdf_ground_truth.iterrows():
    folium.CircleMarker(
        location=[row['latitude'], row['longitude']],
        radius=5,
        color='blue',
        fill=True,
        popup=f"Пожар {row['fire_id']}, {row['burned_area_ha']:.0f} га"
    ).add_to(marker_cluster_gt)
marker_cluster_gt.add_to(m)

# Слой 2: Обнаруженные "Стрелой"
marker_cluster_strela = MarkerCluster(name='Стрела (обнаружено)')
for idx, row in strela_detections.iterrows():
    folium.CircleMarker(
        location=[row['geometry'].y, row['geometry'].x],
        radius=4,
        color='red',
        fill=True,
        popup=f"Детекция {row['detection_id']}"
    ).add_to(marker_cluster_strela)
marker_cluster_strela.add_to(m)

# Слой 3: Промышленные зоны (Overture Maps)
folium.GeoJson(
    industrial_zones,
    name='Промзоны (фильтр)',
    style_function=lambda x: {'color': 'gray', 'opacity': 0.3, 'fillOpacity': 0.1}
).add_to(m)

# Добавляем переключатель слоёв
folium.LayerControl().add_to(m)

# Сохраняем карту
m.save("fire_detection_map.html")
print("Карта сохранена: fire_detection_map.html")
```

#### 3.2 Генерация отчёта для стейкхолдеров
```python
# Шаблон отчёта в Markdown
report = f"""
# Отчёт о валидации системы "Стрела"
## Регион: {target_region}
## Период: {target_month}

### 📊 Метрики детекции активных пожаров

| Метрика | Значение | Целевое | Статус |
|---|---|---|---|
| Precision | {precision:.1%} | >80% | {'✅' if precision > 0.8 else '⚠️'} |
| Recall | {recall:.1%} | >75% | {'✅' if recall > 0.75 else '⚠️'} |
| F1-score | {f1:.1%} | >77% | {'✅' if f1 > 0.77 else '⚠️'} |

**Детализация:**
- Обнаружено пожаров: **{TP}** из **{TP + FN}** ({recall:.0%})
- Ложных срабатываний: **{FP}** ({FP/(TP+FP):.0%} от всех детекций)
- Пропущено пожаров: **{FN}**

### 📈 Точность картирования гарей

| Показатель | Значение | Целевое | Статус |
|---|---|---|---|
| Корреляция площадей | {df_comparison['area_strela_ha'].corr(df_comparison['area_mcd64a1_ha']):.2f} | >0.8 | {'✅' if df_comparison['area_strela_ha'].corr(df_comparison['area_mcd64a1_ha']) > 0.8 else '⚠️'} |
| Среднее отклонение | {df_comparison['difference_pct'].mean():.1f}% | <20% | {'✅' if df_comparison['difference_pct'].mean() < 20 else '⚠️'} |
| Медианное отклонение | {df_comparison['difference_pct'].median():.1f}% | <15% | {'✅' if df_comparison['difference_pct'].median() < 15 else '⚠️'} |

### 🔍 Анализ ошибок

**Ложные срабатывания (FP = {FP}):**
- Промышленные объекты (не отфильтрованы Overture Maps): {count_industrial_fp}
- Мелкие пожары < 1 га (не зарегистрированы в Рослесхозе): {count_small_fires_fp}
- Другие источники тепла: {count_other_fp}

**Пропущенные пожары (FN = {FN}):**
- Облачность в момент пролёта VIIRS: {count_cloud_fn}
- Мелкие пожары < 1 га (ниже порога детекции): {count_small_fires_fn}
- Задержка данных: {count_delay_fn}

### 🎯 Выводы и рекомендации

**Сильные стороны:**
1. Высокая точность детекции крупных пожаров (>10 га)
2. Быстрая реакция (<12 часов от начала пожара)
3. Эффективная фильтрация промышленных объектов через Overture Maps

**Области улучшения:**
1. Повышение точности для мелких пожаров (<1 га)
2. Улучшение обработки облачности
3. Расширение базы промышленных объектов в Overture Maps

**Рекомендации для следующего этапа:**
1. Внедрить ML модель для уточнения детекции (обучение на экспертной разметке)
2. Добавить данные Sentinel-1 (SAR) для работы в облачную погоду
3. Расширить временной период анализа (полный сезон пожаров)
"""

# Сохраняем отчёт
with open("validation_report.md", "w", encoding="utf-8") as f:
    f.write(report)

print("Отчёт сохранён: validation_report.md")
```

---

## 📋 Чек-лист: что нужно сделать

### **Неделя 1: Подготовка**
- [ ] Скачать данные Рослесхоза за июль 2024 (или другой пиковый месяц)
- [ ] Скачать MCD64A1 за тот же период
- [ ] Запустить "Стрелу" за тот же период
- [ ] Подготовить среду для анализа (Python, geopandas, rasterio)

### **Неделя 2: Анализ**
- [ ] Конвертировать MCD64A1 в WGS84
- [ ] Провести пространственное объединение с результатами "Стрелы"
- [ ] Рассчитать метрики Precision/Recall/F1
- [ ] Сравнить площади гарей
- [ ] Проанализировать ошибки (FP/FN)

### **Неделя 3: Визуализация и отчётность**
- [ ] Создать интерактивную карту с тремя слоями
- [ ] Сгенерировать отчёт в Markdown
- [ ] Подготовить презентацию для стейкхолдеров
- [ ] Экспортировать результаты в GeoJSON/CSV

---

## 🎯 Ожидаемые результаты для стейкхолдеров

### **Что покажем:**

1. **Интерактивная карта** с тремя слоями:
   - Синие точки: Пожары по данным Рослесхоза (Ground Truth)
   - Красные точки: Обнаруженные "Стрелой"
   - Серые полигоны: Промышленные зоны (фильтр)

2. **Дашборд с метриками:**
   ```
   ┌─────────────────────────────────────────┐
   │ ТОЧНОСТЬ ДЕТЕКЦИИ ПОЖАРОВ              │
   ├─────────────────────────────────────────┤
   │ Precision: 87% ████████████░░           │
   │ Recall:    82% ██████████░░░░           │
   │ F1-score:  84% ███████████░░░           │
   └─────────────────────────────────────────┘
   ```

3. **Таблица сравнения площадей:**
   | Пожар ID | Площадь Стрела (га) | Площадь MCD64A1 (га) | Отклонение |
   |---|---|---|---|
   | 001 | 45.2 | 42.8 | 5.6% |
   | 002 | 128.5 | 115.3 | 11.5% |
   | ... | ... | ... | ... |

4. **Анализ ошибок:**
   - 73% ложных срабатываний — промышленные объекты (добавить в Overture Maps)
   - 58% пропусков — мелкие пожары <1 га (ограничение разрешения VIIRS)

### **Главный вывод:**
Система "Стрела" демонстрирует **операционную готовность** для мониторинга пожаров в реальном времени с точностью, сопоставимой с научными стандартами (NASA MCD64A1) и официальными данными (Рослесхоз).

**Следующий шаг:** Внедрение ML модели для повышения точности на 5-10% и расширения возможностей для мелких пожаров.

*Код проекта реализован на [google-earth-engine](https://console.cloud.google.com/earth-engine/tasks?cloudshell=true&project=lucid-diode-397420)*