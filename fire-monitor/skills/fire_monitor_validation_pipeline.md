Этот документ формализует процесс валидации системы «Стрела», жёстко задаёт геодезические правила (чтобы агент не ошибался с проекциями на высоких широтах) и описывает алгоритм расчёта метрик.

---

# 📄 SKILL.md: `fire_monitor_validation_pipeline`

## 🤖 Agent Profile
- **Agent Name:** Qwen Code Companion
- **Model:** Qwen 3.8 - 32b
- **Role:** Senior Geospatial Data Engineer & ML Evaluator
- **Domain:** Remote Sensing, Wildfire Monitoring, GIS, Earth Observation
- **Project Context:** GeoAi-agent_fire_space (Система «Стрела»)

---

## 🎯 Objective
Automate the quantitative evaluation of the "Strela" wildfire monitoring system. The agent must fetch ground truth data, align coordinate reference systems (CRS), handle temporal tolerances, and calculate standard binary classification metrics (Precision, Recall, F1) for active fire detection, alongside spatial metrics (IoU, Area Correlation) for burn scar mapping.

---

## 📥 Inputs
1. **System Outputs (Strela):**
   - `viirs_detections.geojson` (Points: active fire hotspots, post-Overture Maps filtering)
   - `burned_areas.geojson` (Polygons: Sentinel-2 dNBR burn scars)
2. **Target Parameters:**
   - `region` (e.g., "Иркутская область", "Якутия")
   - `start_date` / `end_date` (e.g., "2024-07-01" to "2024-07-31")
3. **External Ground Truth APIs / URLs:**
   - Rosleshoz ISDM (via `tochno.st` CSV dumps)
   - NASA LAADS DAAC (for MCD64A1 HDF-EOS tiles)

---

## 🛠️ Required Toolchain
- **Geospatial:** `geopandas`, `shapely`, `pyproj`, `rasterio`, `rioxarray`
- **Data Processing:** `pandas`, `numpy`, `xarray`
- **API & I/O:** `requests`, `aiohttp`, `h5py`
- **Visualization:** `folium`, `matplotlib`

---

## ⚙️ Execution Workflow

### Phase 1: Data Ingestion & Harmonization
1. **Fetch Rosleshoz Registry (1T-ISDM):**
   - Download CSV from `tochno.st/datasets/fires`.
   - Filter by `region` and `fire_start_date`.
   - *Constraint:* Handle `;` separator and `utf-8` encoding. Drop rows with missing coordinates.
2. **Fetch NASA MCD64A1 (MODIS Burned Area):**
   - Calculate intersecting sinusoidal tiles for the target region.
   - Download `.hdf` files for the target month.
   - **CRITICAL STEP:** Reproject MCD64A1 from Sinusoidal to **EPSG:4326 (WGS84)** using `gdal.Warp` or `rioxarray`. Use `bilinear` for Burn Date and `nearest` for QA layers.
3. **Load Strela Outputs:**
   - Read GeoJSONs into GeoDataFrames (GDF). Ensure CRS is EPSG:4326.

### Phase 2: Active Fire Validation (Point-to-Point)
*Comparing Strela VIIRS detections vs Rosleshoz ISDM registry.*
1. **Geodesic Buffering:**
   - Do NOT use simple degree buffers. Use `pyproj.Geod` or `shapely` with UTM/Albers Equal Area projection to create a **500m geodesic buffer** around Rosleshoz points (accounts for GPS inaccuracies and VIIRS 375m resolution).
2. **Spatial Join:**
   - `sjoin(rosleshoz_buffered, strela_detections, predicate='intersects')`
3. **Temporal Matching:**
   - Match only if `|strela_detection_time - rosleshoz_start_date| <= 3 days`.
4. **Metric Calculation:**
   - **TP (True Positive):** Matched detections.
   - **FP (False Positive):** Strela detections with no Rosleshoz match (analyze if they fall outside Overture Maps industrial zones).
   - **FN (False Negative):** Rosleshoz fires with no Strela detection.
   - *Formulas:* `Precision = TP / (TP + FP)`, `Recall = TP / (TP + FN)`, `F1 = 2 * (P * R) / (P + R)`.

### Phase 3: Burn Scar Mapping Validation (Polygon-to-Raster)
*Comparing Strela Sentinel-2 dNBR vs NASA MCD64A1.*
1. **QA Filtering (MCD64A1):**
   - Read the QA layer. Apply bitmask to keep only valid land pixels: `(QA & 0b00000011) == 3` (Bit 0: Land, Bit 1: Valid Data).
   - Exclude water bodies and persistent hotspots (Bits 5-7).
2. **Rasterization:**
   - Rasterize Strela `burned_areas.geojson` to match the exact transform, shape, and CRS of the reprojected MCD64A1 raster.
3. **Spatial Metrics:**
   - Calculate **Intersection over Union (IoU)** for burned pixels.
   - Calculate **Pearson Correlation** between Strela polygon areas (ha) and MCD64A1 pixel counts (converted to ha).
   - Calculate **Temporal Lag:** `Mean(Strela_Mapping_Date - MCD64A1_Burn_Date)`.

### Phase 4: Error Analysis & Reporting
1. **FP Categorization:** Cross-reference FPs with Overture Maps. If an FP is near an industrial zone but outside the 500m buffer, flag it as "Buffer Expansion Needed".
2. **FN Categorization:** Check Sentinel-2 cloud masks (SCL). If FN occurred during high cloud cover, flag as "Sensor Limitation (Optical)".
3. **Generate Artifacts:**
   - `validation_report.md` (Markdown summary with tables).
   - `validation_map.html` (Folium interactive map with Ground Truth, Strela, and Industrial layers).

---

## ⚠️ Domain Constraints & Geospatial Rules (Agent Directives)

1. **High Latitude Distortion:** Russia spans high latitudes. **NEVER** calculate areas in EPSG:4326 (degrees). Always project to an equal-area CRS (e.g., `EPSG:3576` - WGS 84 / North Pole LAEA Russia or local UTM) before calculating hectares.
2. **Temporal Tolerance:** VIIRS detects active fires in NRT (hours). MCD64A1 is a monthly composite that relies on temporal BRDF models. A burn scar mapped by Sentinel-2 on July 15 might have a Burn Date of July 12 in MCD64A1. Always use a **±5 day temporal window** for burn area matching.
3. **Resolution Mismatch:** VIIRS (375m) and MODIS (500m) will miss sub-pixel fires (< 1 ha). Rosleshoz registry contains fires down to 0.1 ha. FPs in Strela might actually be unregistered small fires. Document this in the "Limitations" section of the report.
4. **Overture Maps Pre-filtering:** Assume Strela's FPs have *already* been filtered by Overture Maps. If FPs remain high, the agent must suggest updating the Overture Maps cache or increasing the industrial buffer from 500m to 1000m.
5. **MCD64A1 Bitmasking:** The QA layer in MCD64A1 is notoriously tricky. Bit 0 is water/land, Bit 1 is valid/missing. Always use bitwise operations: `is_valid = (qa_array & 2) > 0` and `is_land = (qa_array & 1) > 0`.

---

## 🚀 Trigger Commands

The agent should activate this skill upon receiving prompts like:
- `@agent run_validation --region "Иркутская область" --period "2024-07"`
- `@agent calculate_metrics --ground-truth rosleshoz --target viirs_detections.geojson`
- `@agent generate_stakeholder_report`

---

## 📊 Expected Output Format (Markdown Report Template)

```markdown
# 📊 Validation Report: "Strela" System
**Region:** {region} | **Period:** {period}

## 1. Active Fire Detection (VIIRS vs Rosleshoz)
| Metric | Value | Target | Status |
|---|---|---|---|
| Precision | {precision}% | >80% | {status} |
| Recall | {recall}% | >75% | {status} |
| F1-Score | {f1}% | >77% | {status} |

* **True Positives:** {TP}
* **False Positives:** {FP} (Recommendation: {fp_action})
* **False Negatives:** {FN} (Primary cause: {fn_cause})

## 2. Burn Scar Mapping (Sentinel-2 vs MCD64A1)
* **Total Burned Area (Strela):** {area_strela} ha
* **Total Burned Area (MCD64A1):** {area_modis} ha
* **Spatial Correlation (r):** {correlation}
* **Mean Temporal Lag:** {lag} days

## 3. System Limitations & Recommendations
1. ...
2. ...
```

---

## 🧠 Context Memory (For Future ML Integration)
*Note to Agent:* The current pipeline relies purely on heuristic and deterministic spatial joins. In future iterations, integrate the **Cosmohack Dataset** (`fire-train-renamed/train/af` and `bs`) to train a U-Net/ResNet model. The `af` masks (VIIRS I1-I5) will replace the static temperature thresholds, and `bs` masks (Sentinel-1/2 pre/post) will replace the static dNBR thresholds, expected to increase F1-score by 5-10%. Keep this in mind when analyzing False Positives.