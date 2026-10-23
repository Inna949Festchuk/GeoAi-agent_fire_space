"""
Validation artifacts: Markdown report, Folium map, metrics CSV.

Fixed file names (per firstpromt.md), overwritten on each run:
  reports/validation_report.md
  reports/validation_map.html
  reports/metrics_summary.csv
"""
import csv
import logging
from pathlib import Path

import pandas as pd

from .config import TARGETS

logger = logging.getLogger(__name__)

REPORT_MD = 'validation_report.md'
REPORT_MAP = 'validation_map.html'
REPORT_CSV = 'metrics_summary.csv'


def write_reports(result, out_dir):
    """Write all artifacts. Returns {artifact_name: Path}."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}

    paths[REPORT_MD] = out_dir / REPORT_MD
    _write_markdown(result, paths[REPORT_MD])

    paths[REPORT_CSV] = out_dir / REPORT_CSV
    _write_csv(result, paths[REPORT_CSV])

    try:
        paths[REPORT_MAP] = out_dir / REPORT_MAP
        _write_map(result, paths[REPORT_MAP])
    except Exception as e:
        logger.exception('Folium map generation failed')
        result.setdefault('warnings', []).append(f'Map generation failed: {e}')
        paths.pop(REPORT_MAP, None)

    return paths


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct(x):
    return 'n/d' if x is None else f'{x * 100:.1f}%'


def _status(value, target, higher_is_better=True):
    if value is None:
        return '—'
    ok = value >= target if higher_is_better else value <= target
    return '✅ мет' if ok else '⚠️ не мет'


def _plural(n, one, few, many):
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def _write_markdown(result, path):
    region = result.get('region_name') or f"bbox {result['bbox']}"
    src = result.get('source') or 'auto'
    src_label = 'files' if src == 'files' else 'база данных'

    lines = []
    lines.append('# 📊 Отчёт о валидации системы «Стрела»')
    lines.append('')
    lines.append(f'**Регион:** {region}  ')
    lines.append(f'**Период:** {result["start_date"]} — {result["end_date"]}  ')
    lines.append(f'**Сгенерирован:** {result["generated_at"]}  ')
    dd = result.get('dedupe')
    if dd:
        strela_data = (f'Стрела: **{dd["clusters"]:,}** уникальных очагов '
                       f'(из **{dd["raw"]:,}** сырых детекций), '
                       f'**{result["strela_burn_polygons"]}** полигонов гарей')
    else:
        strela_data = (f'Стрела: **{result["strela_fires"]}** детекций, '
                       f'**{result["strela_burn_polygons"]}** полигонов гарей')
    lines.append(f'**Данные:** Рослесхоз (GT): **{result["gt_fires"]}** пожаров | '
                 f'{strela_data}')
    lines.append('')

    # 1. Detection
    lines.append('## 1. Детекция активных пожаров (Стрела vs Рослесхоз 1Т-ИСДМ)')
    lines.append('')
    d = result.get('detection')
    if d:
        lines.append('| Метрика | Значение | Цель | Статус |')
        lines.append('|---|---|---|---|')
        lines.append(f"| Precision | {_pct(d['precision'])} | >{TARGETS['precision']:.0%} | "
                     f"{_status(d['precision'], TARGETS['precision'])} |")
        lines.append(f"| Recall | {_pct(d['recall'])} | >{TARGETS['recall']:.0%} | "
                     f"{_status(d['recall'], TARGETS['recall'])} |")
        lines.append(f"| F1-Score | {_pct(d['f1'])} | >{TARGETS['f1']:.0%} | "
                     f"{_status(d['f1'], TARGETS['f1'])} |")
        lines.append('')
        lines.append(f"- True Positives (TP): **{d['tp']}**")
        lines.append(f"- False Positives (FP): **{d['fp']}** "
                     '(обнаружено Стрелой, нет в реестре Рослесхоза)')
        lines.append(f"- False Negatives (FN): **{d['fn']}** "
                     '(есть в реестре, Стрела не обнаружила)')
        window_note = (f"±{d['time_window_days']} дней"
                       if not result.get('dedupe')
                       else (f"±{d['time_window_days']} дней от периода "
                             'активности очага'))
        lines.append(f"- Буфер: {d['buffer_m']:.0f} м геодезический; "
                     f"временной допуск: {window_note}")
        dd = result.get('dedupe')
        if dd:
            lines.append(
                f"- Дедупликация: {dd['raw']:,} сырых детекций → "
                f"{dd['clusters']:,} уникальных очагов "
                f"({dd['distance_m']:.0f} м, ±{dd['time_window_days']:g} дн.) — "
                'метрики считаются по очагам, а не по каждому пролёту спутника')
    else:
        lines.append('_Не рассчитана — нет сопоставимых данных (см. ограничения)._')
    lines.append('')

    # 2. Burn scars
    lines.append('## 2. Картирование гарей (Стрела Sentinel-2 dNBR vs NASA MCD64A1)')
    lines.append('')
    b = result.get('burns')
    if b:
        corr_s = 'n/d' if b['area_correlation'] is None else f"{b['area_correlation']:.2f}"
        dev_s = 'n/d' if b['mean_area_deviation_pct'] is None else f"{b['mean_area_deviation_pct']:.1f}%"
        lag_s = 'n/d' if b['temporal_lag_days'] is None else f"{b['temporal_lag_days']:.1f}"
        iou_status = _status(b['iou'], TARGETS['burn_iou'])
        f1_status = _status(b.get('burn_f1'), TARGETS['burn_f1'])
        corr_status = _status(b['area_correlation'], TARGETS['burn_area_correlation'])
        dev_status = _status(b['mean_area_deviation_pct'],
                             TARGETS['burn_mean_area_deviation_pct'],
                             higher_is_better=False)
        f1_s = 'n/d' if b.get('burn_f1') is None else f"{b['burn_f1']:.2f}"
        lines.append('| Метрика | Значение | Цель | Статус |')
        lines.append('|---|---|---|---|')
        lines.append(f"| IoU (пиксели выгорания) | {b['iou']:.2f} | "
                     f">{TARGETS['burn_iou']} | {iou_status} |")
        lines.append(f"| F1 (пиксели выгорания) | {f1_s} | "
                     f">{TARGETS['burn_f1']} | {f1_status} |")
        lines.append(f"| Корреляция площадей (Пирсон r) | {corr_s} | "
                     f">{TARGETS['burn_area_correlation']} | {corr_status} |")
        lines.append(f"| Среднее отклонение площадей | {dev_s} | "
                     f"<{TARGETS['burn_mean_area_deviation_pct']:.0f}% | {dev_status} |")
        lines.append('')
        lines.append(f"- Общая площадь гарей (Стрела): **{b['strela_area_ha']:,.0f} га**")
        lines.append(f"- Общая площадь гарей (MCD64A1, в допуске): "
                     f"**{b['modis_area_ha']:,.0f} га**")
        lines.append(f"- Средний временной лаг: **{lag_s} дн.**")
        lines.append(f"- Полигонов гарей сравнивается: **{b['polygons_compared']}**")
        lines.append(f"- Сетка растра MCD64A1: {b['resolution_m']:.0f} м; "
                     f"допуск по дате выгорания ±{b['time_window_days']} дней")
        if b.get('buffer_m'):
            lines.append(f"- Буфер полигонов Стрела: {b['buffer_m']:.0f} м "
                         '(компенсация размывания границ dNBR)')
    else:
        lines.append('_Не рассчитана — нет сопоставимых данных (см. ограничения)._')
    lines.append('')

    # 3. FP analysis
    fi = result.get('fp_industrial')
    if fi:
        lines.append('## 3. Анализ ложных срабатываний (промзоны)')
        lines.append('')
        if fi.get('note'):
            lines.append(f"- {fi['note']}")
        lines.append(f"- Промышленных зон в районе (Overture Maps): {fi['zones_in_area']}")
        lines.append('')

    # 4. Limitations
    lines.append('## 4. Ограничения')
    lines.append('')
    lines.append('1. VIIRS (375 м) и MODIS (500 м) пропускают субпиксельные пожары '
                 '(<1 га); реестр Рослесхоза содержит пожары от 0.1 га — часть FP '
                 'может быть незарегистрированными мелкими пожарами.')
    lines.append('2. MCD64A1 — месячный композитный продукт с временной BRDF-моделью: '
                 'дата выгорания может отличаться на несколько дней.')
    lines.append('3. Реестр Рослесхоза содержит только зарегистрированные (тушенные) '
                 'пожары; координаты приблизительные (точность ~100–500 м).')
    lines.append('4. MCD64A1 (500 м) может не видеть мелкие пожары <25 га.')
    lines.append('')

    if result.get('warnings'):
        lines.append('## 5. Предупреждения')
        lines.append('')
        for w in result['warnings']:
            lines.append(f'- {w}')
        lines.append('')

    lines.append('## Данные')
    lines.append('')
    lines.append('- Ground truth: реестр Рослесхоза 1Т-ИСДМ (tochno.st), '
                 'NASA MCD64A1 6.1 (LAADS DAAC)')
    lines.append(f'- Стрела: детекции и полигоны гарей системы '
                 f'(источник: {src_label})')
    path.write_text('\n'.join(lines), encoding='utf-8')


def _write_csv(result, path):
    rows = []
    d = result.get('detection')
    if d:
        for name, key, target, hib in (
            ('precision', 'precision', TARGETS['precision'], True),
            ('recall', 'recall', TARGETS['recall'], True),
            ('f1', 'f1', TARGETS['f1'], True),
        ):
            v = d[key]
            rows.append([name, f'{v:.4f}', f">={target}",
                         'met' if (v >= target) == hib else 'not met'])
        rows.append(['tp', d['tp'], '', ''])
        rows.append(['fp', d['fp'], '', ''])
        rows.append(['fn', d['fn'], '', ''])
    b = result.get('burns')
    if b:
        if b['iou'] is not None:
            rows.append(['burn_iou', f"{b['iou']:.4f}", f">{TARGETS['burn_iou']}",
                         'met' if b['iou'] >= TARGETS['burn_iou'] else 'not met'])
        if b.get('burn_f1') is not None:
            rows.append(['burn_f1', f"{b['burn_f1']:.4f}", f">{TARGETS['burn_f1']}",
                         'met' if b['burn_f1'] >= TARGETS['burn_f1'] else 'not met'])
        if b['area_correlation'] is not None:
            rows.append(['burn_area_correlation', f"{b['area_correlation']:.4f}",
                         f">{TARGETS['burn_area_correlation']}",
                         'met' if b['area_correlation'] >= TARGETS['burn_area_correlation']
                         else 'not met'])
        if b['mean_area_deviation_pct'] is not None:
            rows.append(['burn_mean_area_deviation_pct',
                         f"{b['mean_area_deviation_pct']:.1f}",
                         f"<{TARGETS['burn_mean_area_deviation_pct']}",
                         'met' if b['mean_area_deviation_pct']
                         <= TARGETS['burn_mean_area_deviation_pct'] else 'not met'])
        rows.append(['burn_strela_area_ha', f"{b['strela_area_ha']:.1f}", '', ''])
        rows.append(['burn_modis_area_ha', f"{b['modis_area_ha']:.1f}", '', ''])
        rows.append(['burn_temporal_lag_days',
                     'n/d' if b['temporal_lag_days'] is None else f"{b['temporal_lag_days']:.2f}",
                     '', ''])
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['metric', 'value', 'target', 'status'])
        w.writerows(rows)


def _write_map(result, path):
    import folium
    from folium.plugins import MarkerCluster

    bbox = result['bbox']
    center = [(bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2]
    m = folium.Map(location=center, zoom_start=7, tiles='OpenStreetMap')
    geo = result.get('_geo') or {}
    has_layer = False

    gt = geo.get('gt')
    if gt is not None and not gt.empty:
        cluster = MarkerCluster(name='Рослесхоз (Ground Truth)', icon='blue')
        for _, row in gt.iterrows():
            c = row.geometry.centroid
            date_s = (str(row.get('fire_start_date'))[:10]
                      if pd.notna(row.get('fire_start_date')) else '')
            area_s = (f"{row.get('area_ha'):.0f} га"
                      if pd.notna(row.get('area_ha')) else '')
            folium.CircleMarker(
                location=[c.y, c.x], radius=5, color='#1f77b4',
                fill=True, fill_opacity=0.8,
                popup=f'Пожар Рослесхоза: {date_s}, {area_s}',
            ).add_to(cluster)
        cluster.add_to(m)
        has_layer = True

    strela = geo.get('strela_fires')
    if strela is not None and not strela.empty:
        is_spots = 'cluster_size' in strela.columns
        cluster = MarkerCluster(
            name='Стрела (уникальные очаги)' if is_spots
            else 'Стрела (обнаружено)',
            icon='red')
        for _, row in strela.iterrows():
            c = row.geometry.centroid
            date_s = (str(row.get('detected_at'))[:10]
                      if pd.notna(row.get('detected_at')) else '')
            frp_s = (f"{row.get('frp'):.1f} МВт"
                     if pd.notna(row.get('frp')) else '')
            spot_s = ''
            if is_spots and pd.notna(row.get('cluster_size')):
                last_s = str(row.get('last_at'))[:10]
                size = int(row['cluster_size'])
                spot_s = (f', очаг: {size} '
                          + _plural(size, 'детекция', 'детекции', 'детекций')
                          + (f' {date_s}–{last_s}'
                             if last_s and last_s > date_s else ''))
            folium.CircleMarker(
                location=[c.y, c.x], radius=4, color='#d62728',
                fill=True, fill_opacity=0.8,
                popup=f'Детекция Стрелы: {date_s}, FRP {frp_s}{spot_s}',
            ).add_to(cluster)
        cluster.add_to(m)
        has_layer = True

    burns = geo.get('strela_burns')
    if burns is not None and not burns.empty:
        # datetime columns break folium's to_json — drop them (keep geometry!)
        burn_cols = [c for c in burns.columns
                     if not pd.api.types.is_datetime64_any_dtype(burns[c])]
        folium.GeoJson(
            burns[burn_cols].to_json(),
            style_function=lambda x: {'color': 'orange', 'weight': 1.5,
                                      'fillColor': 'orange', 'fillOpacity': 0.25},
            name='Стрела (гари)',
        ).add_to(m)
        has_layer = True

    modis = geo.get('modis_burn')
    if modis is not None and not modis.empty:
        folium.GeoJson(
            modis.to_json(),
            style_function=lambda x: {'color': '#15803d', 'weight': 1.5,
                                      'fillColor': '#16a34a', 'fillOpacity': 0.2},
            name='NASA MCD64A1 (эталон)',
        ).add_to(m)
        has_layer = True

    zones = geo.get('industrial_zones')
    if zones is not None and not zones.empty:
        folium.GeoJson(
            zones.to_json(),
            style_function=lambda x: {'color': 'gray', 'weight': 1,
                                      'fillColor': 'gray', 'fillOpacity': 0.1},
            name='Промзоны (фильтр)',
        ).add_to(m)
        has_layer = True

    if has_layer:
        folium.LayerControl().add_to(m)
    m.save(str(path))