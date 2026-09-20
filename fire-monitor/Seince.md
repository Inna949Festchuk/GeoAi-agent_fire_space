
# Научное обоснование: Определение уровня уверенности пожаров

## Источник данных

Уровень уверенности (confidence) определяется **NASA FIRMS** (Fire Information for Resource Management System) на основе алгоритмов обработки данных спутниковых сенсоров VIIRS и MODIS.

## Инструменты определения

### 1. VIIRS (Visible Infrared Imaging Radiometer Suite)

**Спутники:** Suomi NPP, NOAA-20  
**Разрешение:** 375 метров  
**Каналы:**
- **I4 (10.76 мкм)** — термальный инфракрасный канал для обнаружения горячих точек
- **I5 (11.45 мкм)** — дополнительный термальный канал

**Алгоритм определения confidence:**

VIIRS использует алгоритм на основе **контекстного анализа**:

1. **Абсолютная температура яркости (Brightness Temperature)**
   - Измеряется в каналах I4 и I5
   - Пороговые значения:
     - День: > 330K (57°C)
     - Ночь: > 290K (17°C)

2. **Контраст с окружающими пикселями**
   - Алгоритм сравнивает температуру целевого пикселя с фоном (8-окрестность)
   - Если разница > 30K — высокая уверенность
   - Если разница 10-30K — номинальная уверенность
   - Если разница < 10K — низкая уверенность

3. **Спектральная сигнатура**
   - Соотношение между каналами I4 и I5
   - Пожары имеют характерную сигнатуру в термальном ИК-диапазоне

**Классификация VIIRS:**
- `high` — температура > 360K И контраст > 30K
- `nominal` — температура 330-360K ИЛИ контраст 10-30K
- `low` — температура близка к порогу ИЛИ контраст < 10K

### 2. MODIS (Moderate Resolution Imaging Spectroradiometer)

**Спутники:** Terra, Aqua  
**Разрешение:** 1 км  
**Каналы:**
- **Band 21/22 (3.9 мкм)** — средний инфракрасный (чувствителен к пожарам)
- **Band 31 (11 мкм)** — термальный инфракрасный

**Алгоритм определения confidence:**

MODIS использует более сложный алгоритм с **числовой оценкой (0-100)**:

1. **Температура яркости в 3.9 мкм (T31)**
   - Более чувствителен к малым пожарам
   - Порог: > 350K днём, > 300K ночью

2. **Температура яркости в 11 мкм (T4)**
   - Основной термальный канал
   - Порог: > 310K днём, > 290K ночью

3. **Контекстные тесты**
   - Контраст с окружающими пикселями
   - Проверка на облачность
   - Анализ водных поверхностей
   - Проверка пустынных регионов

4. **Числовая оценка (0-100)**
   - Комбинация всех тестов даёт итоговый балл
   - >= 80 → `high` (высокая уверенность)
   - 40-79 → `nominal` (номинальная уверенность)
   - < 40 → `low` (низкая уверенность)

## Физическая основа

### Почему эти каналы работают?

**Пожары излучают в двух основных диапазонах:**

1. **Средний ИК (3.9 мкм)**
   - Пик излучения для температур 500-1000K
   - Высокая чувствительность к малым пожарам
   - Проблема: солнечные блики днём

2. **Термальный ИК (11 мкм)**
   - Излучение нагретых поверхностей
   - Работает днём и ночью
   - Менее чувствителен к малым пожарам

### Формула Planck

Яркость излучения описывается законом Планка:

```
B(λ,T) = (2hc²/λ⁵) × 1/(e^(hc/λkT) - 1)
```

где:
- λ — длина волны
- T — температура (K)
- h — постоянная Планка
- c — скорость света
- k — постоянная Больцмана

**Для пожара (T ≈ 800K):**
- При 3.9 мкм: излучение в 10-100 раз сильнее фона
- При 11 мкм: излучение в 2-5 раз сильнее фона

## Источники ложных срабатываний

### 1. Промышленные объекты
- Нефтяные вышки (gas flaring)
- Металлургические заводы
- Электростанции

**Как отличить:** Постоянная локация, стабильная температура

### 2. Солнечные блики
- Отражения от крыш, воды, стекла
- Только днём

**Как отличить:** Проверка соотношения каналов 3.9 мкм / 11 мкм

### 3. Вулканическая активность
- Лава, горячие газы

**Как отличить:** FIRMS маркирует как `type=3` (volcanic)

### 4. Сельскохозяйственные палы
- Намеренное выжигание

**Как отличить:** Сложно, требуется дополнительный контекст (land use)

## Фильтрация в нашем приложении

После получения данных от NASA FIRMS, наше приложение применяет **дополнительную фильтрацию**:

### Критерии отсева:

1. **Температурный порог**
   - VIIRS: < 330K (день), < 290K (ночь)
   - MODIS: < 310K (день), < 290K (ночь)

2. **Минимальная мощность пожара (FRP)**
   - VIIRS: < 3 MW
   - MODIS: < 5 MW
   - Исключение: если confidence = 'high'

3. **Промышленные зоны (Overture Maps)**
   - **Spatial join** с базой данных промышленных зон
   - Проверка попадания точки в промзону + буфер 500м
   - Если FRP < 50 MW и confidence != 'high' → отсев
   - Типы объектов: oil_and_gas, power_plant, quarry, mine, industrial, factory, warehouse
   - Данные загружаются из Overture Maps и кэшируются в PostGIS

4. **Тип пожара (FIRMS)**
   - `type=2` → oil/gas flare (отсев)
   - `type=3` → volcanic (отсев)

### Преимущества Overture Maps:

- ✅ Актуальные данные (обновляются ежемесячно)
- ✅ Покрытие всего мира
- ✅ Точная геометрия объектов
- ✅ Динамическое обновление кэша

## Статистика точности

По данным NASA:
- **VIIRS:** ~90% точность обнаружения пожаров > 1 га
- **MODIS:** ~80% точность обнаружения пожаров > 5 га
- **Ложные срабатывания:** 10-30% (зависит от региона)

## Ссылки

- [NASA FIRMS Documentation](https://firms.modaps.eosdis.nasa.gov/api/)
- [VIIRS Active Fire Algorithm](https://www.ospo.noaa.gov/Products/land/af.html)
- [MODIS Fire Product](https://modis.gsfc.nasa.gov/data/dataprod/mod14.php)
- [Giglio et al. (2003) - MODIS fire detection algorithm](https://doi.org/10.1080/01431160304951)

# Дополнительная справка
Актуальные термальные аномалии обновляются в режиме реального времени, и посмотреть их можно непосредственно на интерактивной [официальной карте NASA FIRMS Fire Map](https://firms.modaps.eosdis.nasa.gov/map/). Данные туда поступают со спутников в течение 3 часов с момента пролета. [1, 2] 
## Как расшифровывать термальные аномалии на карте:

* 
* Что фиксируют датчики: Спутники определяют инфракрасное излучение. Красные или оранжевые маркеры на карте указывают на пиксели, где температура поверхности значительно превышает норму. [3, 4] 
* Источники тепловых точек: Это не обязательно лесной или ландшафтный пожар. Система FIRMS фиксирует любые сильные источники тепла: природные и техногенные возгорания, сжигание попутного газа на нефтепромыслах (газовые факелы), извержения вулканов, сельскохозяйственные палы или работу крупных промышленных предприятий. [1, 2, 5] 
* Используемые инструменты: В основе карты лежат данные радиометров MODIS (разрешение пикселя ~1 км) и более современных VIIRS (повышенное разрешение 375 метров, позволяющее находить более мелкие и скрытые очаги). [4, 6] 
* 

## Где отслеживать данные:

   1. Глобальный мониторинг: Основная веб-платформа [NASA LANCE FIRMS](https://firms.modaps.eosdis.nasa.gov/) аккумулирует данные по всему земному шару.
   2. Скачивание данных: Если вам нужны сырые координаты (в форматах TXT, SHP, KML) для интеграции в геоинформационные системы (ГИС), их можно выгрузить в разделе [Active Fire Data на сайте NASA](https://firms.modaps.eosdis.nasa.gov/active_fire/). [7, 8, 9] 

Важное техническое обновление: С 1 ноября 2026 года прекращается поставка данных со старого спутника Suomi NPP, поэтому для актуального анализа FIRMS полностью переходит на использование группировок спутников нового поколения NOAA-20 и NOAA-21. [10] 
Если вы ищете термальные аномалии в конкретном регионе или стране, уточните локацию, и я подскажу, как настроить фильтры или слои карты для этой территории.

[1] [https://www.mdpi.com](https://www.mdpi.com/2624-795X/7/3/90)
[2] [https://firms.modaps.eosdis.nasa.gov](https://firms.modaps.eosdis.nasa.gov/map/)
[3] [https://www.sciencedaily.com](https://www.sciencedaily.com/releases/2026/09/260904000318.htm)
[4] [https://firms.modaps.eosdis.nasa.gov](https://firms.modaps.eosdis.nasa.gov/map/?fbclid=IwY2xjawPlVRRleHRuA2FlbQIxMABicmlkETFDd2d2eXdtVTlpdUFsU0FNc3J0YwZhcHBfaWQQMjIyMDM5MTc4ODIwMDg5MgABHjJ3dG0rXRsV0U9uYBEX0z9FifvE71e1F2rVq5-3yW2xRVd9rpVuQUnkkV7E_aem_dRgnzuE_ouw7zPcLW528zg)
[5] [https://firms.modaps.eosdis.nasa.gov](https://firms.modaps.eosdis.nasa.gov/descriptions/FIRMS_MODIS_Firehotspots.html)
[6] [https://www.earthdata.nasa.gov](https://www.earthdata.nasa.gov/data/tools/firms/faq)
[7] https://firms.modaps.eosdis.nasa.gov
[8] [https://www.earthdata.nasa.gov](https://www.earthdata.nasa.gov/data/catalog/lancemodis-mcd14dl-6.1nrt)
[9] [https://firms.modaps.eosdis.nasa.gov](https://firms.modaps.eosdis.nasa.gov/active_fire/)
[10] [https://firms2.modaps.eosdis.nasa.gov](https://firms2.modaps.eosdis.nasa.gov/map/)

---

# Burn Severity: Оценка степени выгорания лесов

## Обзор метода

**Burn Severity** (степень выгорания) — это количественная оценка воздействия пожара на растительный покров. В нашем приложении используется метод **dNBR (differenced Normalized Burn Ratio)** на основе спутниковых данных Sentinel-2.

## Нормализованный индекс выгорания (NBR)

### Формула NBR

NBR рассчитывается по формуле:

```
NBR = (NIR - SWIR2) / (NIR + SWIR2)
```

где:
- **NIR** (Near Infrared) — ближний инфракрасный канал
  - Sentinel-2: Band 8 (10м разрешение)
  - MODIS: Band 2 (250м разрешение)
- **SWIR2** (Short-Wave Infrared 2) — коротковолновый инфракрасный канал 2
  - Sentinel-2: Band 12 (20м разрешение)
  - MODIS: Band 7 (500м разрешение)

### Физический смысл

**До пожара:**
- Здоровая растительность сильно отражает в NIR (высокое значение)
- Здоровая растительность поглощает в SWIR2 (низкое значение)
- **Результат**: NBR высокий (0.1 — 0.7)

**После пожара:**
- Выгоревшая растительность поглощает в NIR (низкое значение)
- Открытая почва/уголь отражает в SWIR2 (высокое значение)
- **Результат**: NBR низкий (-0.1 — 0.3)

**Разница (dNBR):**
```
dNBR = NBR_pre - NBR_post
```

Чем больше dNBR, тем сильнее выгорание.

## Классификация степени выгорания

### 4-классовая классификация (USGS стандарт)

| Класс | dNBR × 1000 | Описание | Цвет на карте |
|-------|-------------|----------|---------------|
| **Unburned** | < 100 | Не затронуто | 🟢 Зелёный (#65a30d) |
| **Low** | 100 — 269 | Низкая степень | 🟡 Жёлтый (#ca8a04) |
| **Moderate** | 270 — 439 | Средняя степень | 🟠 Оранжевый (#ea580c) |
| **High** | ≥ 440 | Высокая степень | 🔴 Красный (#dc2626) |

### 6-классовая классификация (детальная)

| Класс | dNBR × 1000 | Описание |
|-------|-------------|----------|
| **Enhancement** | < -100 | Усиление вегетации (после пожара) |
| **Unburned** | -100 — 99 | Не затронуто |
| **Low Severity** | 100 — 269 | Низкая степень выгорания |
| **Moderate-Low** | 270 — 439 | Умеренно-низкая степень |
| **Moderate-High** | 440 — 659 | Умеренно-высокая степень |
| **High** | ≥ 660 | Высокая степень выгорания |

## Алгоритм обработки в нашем приложении

### Шаг 1: Поиск спутниковых сцен

```python
# Поиск pre-fire сцены (до пожара)
pre_scenes = search_sentinel2(
    bbox=bbox,
    start_date=pre_date,
    end_date=pre_date,
    max_cloud_cover=30,
    limit=5
)

# Поиск post-fire сцены (после пожара)
post_scenes = search_sentinel2(
    bbox=bbox,
    start_date=post_date,
    end_date=post_date,
    max_cloud_cover=30,
    limit=5
)
```

**Критерии выбора:**
- Минимальная облачность (< 30%)
- Максимальная близость к дате пожара
- Покрытие всей области интереса

### Шаг 2: Загрузка и предобработка данных

```python
# Загрузка каналов B08 (NIR) и B12 (SWIR2)
pre_assets = get_sentinel2_assets(pre_scene, bands=['B08', 'B12'])
post_assets = get_sentinel2_assets(post_scene, bands=['B08', 'B12'])

# Загрузка SCL (Scene Classification Layer) для cloud masking
pre_scl_url = get_scl_asset(pre_scene)
post_scl_url = get_scl_asset(post_scene)

# Чтение данных с клиппингом по bbox
nir_pre = src_nir.read(1, window=window) / 10000  # Нормализация
swir2_pre = src_swir.read(1, window=window) / 10000
```

**Важно:** B12 имеет разрешение 20м, поэтому выполняется ресемплинг до 10м (разрешение B08) с помощью `numpy.repeat`.

### Шаг 2.1: Cloud Masking (маскирование облаков)

```python
# Применение cloud mask с использованием SCL
def apply_cloud_mask(band, scl, cloud_threshold=8):
    """
    Маскирует облака используя Scene Classification Layer.
    
    SCL значения:
    - 8: Cloud medium probability
    - 9: Cloud high probability
    - 10: Thin cirrus
    - 11: Snow (опционально)
    """
    masked = band.copy().astype(float)
    cloud_mask = scl >= cloud_threshold
    masked[cloud_mask] = np.nan
    return masked

# Применение к обоим сценам
if pre_scl_url:
    nir_pre = apply_cloud_mask(nir_pre, scl_pre)
    swir2_pre = apply_cloud_mask(swir2_pre, scl_pre)

if post_scl_url:
    nir_post = apply_cloud_mask(nir_post, scl_post)
    swir2_post = apply_cloud_mask(swir2_post, scl_post)
```

**Преимущества cloud masking:**
- ✅ Автоматическое исключение облачных пикселей
- ✅ Повышение точности анализа
- ✅ Использование официального SCL от Sentinel-2
- ✅ Обработка NaN значений в NBR/dNBR

### Шаг 3: Расчёт NBR и dNBR

```python
# Расчёт NBR для pre-fire сцены
nbr_pre = (nir_pre - swir2_pre) / (nir_pre + swir2_pre)

# Расчёт NBR для post-fire сцены
nbr_post = (nir_post - swir2_post) / (nir_post + swir2_post)

# Расчёт dNBR (масштабирование × 1000 для целочисленного представления)
dnbr = (nbr_pre - nbr_post) * 1000
```

### Шаг 4: Классификация

```python
# Классификация по порогам
severity = np.where(dnbr < 100, 'unburned',
           np.where(dnbr < 270, 'low',
           np.where(dnbr < 440, 'moderate', 'high')))
```

### Шаг 5: Векторизация и оптимизация

```python
# Downsampling 4x для ускорения
mask_downsampled = mask[::4, ::4]

# Векторизация растра в полигоны
for geom, value in shapes(mask_downsampled, transform=transform):
    if value == 1:
        poly = shape(geom)
        if poly.area >= min_area_ha:  # Фильтрация < 1 га
            polygons.append(poly)

# Упрощение геометрий
merged = unary_union(polygons).simplify(0.005)

# Перепроецирование из UTM в WGS84
merged_wgs84 = shapely_transform(project, merged)
```

## Производительность

| Операция | Время | Результат |
|----------|-------|-----------|
| Поиск сцен Sentinel-2 | 2-5 сек | 1-5 сцен |
| Загрузка данных (2 сцены) | 10-15 сек | ~200 МБ |
| Расчёт NBR/dNBR | 3-5 сек | Растр 5000×5000 |
| Векторизация | 5-10 сек | 10-100 полигонов |
| **Итого** | **~35 сек** | **Готовый GeoJSON** |

**Ограничения:**
- Максимальный bbox: 10° × 10° (~1000 км × 1000 км)
- Для больших регионов требуется разбивка на подзадачи

## Точность и валидация

### Сравнение с наземными данными

По исследованиям (Key & Benson, 2006):
- **Общая точность классификации**: 70-85%
- **Высокая степень**: точность ~80%
- **Низкая степень**: точность ~60%

### Источники ошибок

1. **Облачность** — может скрыть часть гарей
2. **Время съёмки** — идеальные условия: 1-3 дня после пожара
3. **Тип растительности** — разные экосистемы имеют разные базовые NBR
4. **Сезонность** — фенологические изменения могут влиять на NBR

## Практическое применение

### Оценка ущерба

- **High severity** (> 440): полная гибель древостоя, требуется лесовосстановление
- **Moderate severity** (270-439): частичная гибель, возможно естественное восстановление
- **Low severity** (100-269): поверхностный пожар, быстрое восстановление

### Планирование мероприятий

- Приоритизация участков для лесовосстановления
- Оценка риска эрозии почв
- Планирование противопожарных мероприятий

### Мониторинг восстановления

- Повторные съёмки через 1, 3, 5 лет
- Оценка эффективности лесовосстановления
- Выявление участков с усиленной вегетацией

## Ссылки

- [USGS Burn Severity Mapping](https://www.usgs.gov/centers/eros/science/burn-severity-mapping)
- [Key, C.H., Benson, N.C. (2006). Landscape Assessment](https://www.fs.usda.gov/rm/pubs_series/rmrs_gtr/rmrs_gtr164.pdf)
- [Sentinel-2 User Handbook](https://sentinel.esa.int/documents/247904/685211/Sentinel-2_User_Handbook)
- [Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/)

---

# Аналитические отчёты

## Обзор системы

Система генерации аналитических отчётов позволяет создавать структурированные summaries по результатам burn severity анализа. Отчёты включают:

- **Severity distribution** — распределение площадей по классам тяжести
- **Summary statistics** — общая площадь, количество полигонов
- **Data quality metrics** — метрики качества данных
- **Land cover breakdown** — анализ по типам земного покрова (в разработке)

## Структура отчёта

### BurnReport model

```python
class BurnReport(models.Model):
    report_id = models.CharField(max_length=50, unique=True)
    generated_at = models.DateTimeField(auto_now_add=True)
    query_params = models.JSONField()  # bbox, date_from, date_to
    summary = models.JSONField()  # общая статистика
    severity_distribution = models.JSONField()  # по классам тяжести
    by_landcover = models.JSONField()  # по типам покрова (опционально)
    data_quality = models.JSONField()  # метрики качества
    burn_areas = models.ManyToManyField(BurnArea)  # связанные полигоны
```

### Пример отчёта

```json
{
  "report_id": "burn_a1b2c3d4e5f6",
  "generated_at": "2025-09-19T14:30:00Z",
  "query_params": {
    "bbox": [79, 71, 82, 74],
    "date_from": "2025-06-01",
    "date_to": "2025-07-31"
  },
  "summary": {
    "total_burn_area_ha": 15420.5,
    "burn_polygons_count": 47,
    "by_severity": {
      "high": {"count": 12, "area_ha": 8230.5},
      "moderate": {"count": 20, "area_ha": 5140.0},
      "low": {"count": 15, "area_ha": 2050.0}
    }
  },
  "severity_distribution": {
    "high": {
      "area_ha": 8230.5,
      "percentage": 53.4,
      "count": 12,
      "mean_dnbr": 542.3
    },
    "moderate": {
      "area_ha": 5140.0,
      "percentage": 33.3,
      "count": 20,
      "mean_dnbr": 345.7
    },
    "low": {
      "area_ha": 2050.0,
      "percentage": 13.3,
      "count": 15,
      "mean_dnbr": 185.2
    }
  },
  "data_quality": {
    "scenes_used": 2,
    "date_count": 2,
    "satellite": "Sentinel-2",
    "resolution_m": 10,
    "notes": "Cloud masking applied using SCL band"
  }
}
```

## Генерация отчётов

### Через API

```bash
# Генерация отчёта
curl -X POST http://localhost:8000/api/reports/generate/ \
  -H "Content-Type: application/json" \
  -d '{
    "bbox": [79, 71, 82, 74],
    "date_from": "2025-06-01",
    "date_to": "2025-07-31"
  }'

# Получение списка отчётов
curl http://localhost:8000/api/reports/

# Получение конкретного отчёта
curl http://localhost:8000/api/reports/<report_id>/

# Экспорт в GeoJSON
curl http://localhost:8000/api/reports/<report_id>/export_geojson/

# Экспорт в CSV
curl http://localhost:8000/api/reports/<report_id>/export_csv/
```

### Через AI чат

```
Пользователь: "Сгенерируй отчёт по гарям в Сибири за июнь-июль 2025"

AI вызывает tool:
{
  "tool": "generate_burn_report",
  "args": {
    "bbox": [79, 71, 109, 101],
    "date_from": "2025-06-01",
    "date_to": "2025-07-31"
  }
}

AI возвращает отчёт с диаграммами и статистикой
```

## Алгоритм генерации

### BurnReportGenerator

```python
class BurnReportGenerator:
    def generate_report(self, bbox, date_from, date_to):
        # 1. Query burn areas from database
        burn_areas = BurnArea.objects.filter(
            geometry__intersects=bbox_geom,
            post_date__gte=date_from,
            post_date__lte=date_to
        )
        
        # 2. Calculate summary statistics
        summary = self.calculate_summary(burn_areas)
        
        # 3. Calculate severity distribution
        severity_distribution = self.calculate_severity_distribution(burn_areas)
        
        # 4. Calculate data quality metrics
        data_quality = self.calculate_data_quality(burn_areas)
        
        # 5. Create report record
        report = BurnReport.objects.create(
            report_id=uuid.uuid4().hex[:12],
            query_params={...},
            summary=summary,
            severity_distribution=severity_distribution,
            data_quality=data_quality
        )
        
        # 6. Link burn areas to report
        report.burn_areas.set(burn_areas)
        
        return report_data
```

### Расчёт severity distribution

```python
def calculate_severity_distribution(self, burn_areas):
    total_area = sum(b.area_ha for b in burn_areas)
    
    distribution = {}
    for severity in ['low', 'moderate', 'high']:
        severity_areas = burn_areas.filter(severity=severity)
        area_ha = sum(b.area_ha for b in severity_areas)
        percentage = (area_ha / total_area * 100) if total_area > 0 else 0
        
        distribution[severity] = {
            'area_ha': round(area_ha, 2),
            'percentage': round(percentage, 1),
            'count': severity_areas.count(),
            'mean_dnbr': severity_areas.aggregate(Avg('dnbr_mean'))
        }
    
    return distribution
```

## Визуализация на фронтенде

### Компонент ResultsVisualization

Отчёты отображаются с помощью круговых диаграмм (PieChart из Recharts):

```jsx
const renderBurnReportChart = (action) => {
  const severityData = Object.entries(severity_distribution)
    .map(([name, data]) => ({
      name: name,
      value: data.area_ha,
      percentage: data.percentage,
      color: getColor(name)
    }))
  
  return (
    <div className="burn-report">
      <PieChart>
        <Pie data={severityData} dataKey="value">
          {severityData.map((entry, index) => (
            <Cell key={index} fill={entry.color} />
          ))}
        </Pie>
        <Tooltip />
        <Legend />
      </PieChart>
      
      <div className="stats">
        <div>Общая площадь: {totalArea} га</div>
        <div>Высокая тяжесть: {highPercentage}%</div>
      </div>
      
      <button onClick={() => exportReport(reportId, 'geojson')}>
        Экспорт GeoJSON
      </button>
    </div>
  )
}
```

## Практическое применение

### Оценка ущерба

- **High severity** (> 50% площади): критический ущерб, требуется срочное лесовосстановление
- **Moderate severity** (30-50%): умеренный ущерб, возможно естественное восстановление
- **Low severity** (< 30%): минимальный ущерб, быстрое восстановление

### Планирование мероприятий

- Приоритизация участков для лесовосстановления
- Оценка необходимого количества саженцев
- Планирование противопожарных мероприятий

### Мониторинг восстановления

- Сравнение отчётов за разные периоды
- Оценка эффективности лесовосстановления
- Выявление участков с усиленной вегетацией

## Производительность

| Операция | Время | Результат |
|----------|-------|-----------|
| Query burn areas | < 1 сек | 10-1000 полигонов |
| Calculate statistics | < 1 сек | Готовый отчёт |
| Экспорт GeoJSON | < 1 сек | Файл 1-10 МБ |
| Экспорт CSV | < 1 сек | Файл 0.1-1 МБ |

## Ссылки

- [Django REST Framework](https://www.django-rest-framework.org/)
- [Recharts](https://recharts.org/)
- [PostGIS ManyToMany](https://docs.djangoproject.com/en/5.0/topics/db/models/#many-to-many-relationships)
