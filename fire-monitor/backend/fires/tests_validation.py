"""
Offline tests for the validation pipeline (metrics + Rosleshoz loader).

No network, no database, no MCD64A1 downloads.
Run: docker compose -f docker-compose.dev.yml exec backend python manage.py test fires.tests_validation
"""
import csv
import math
import tempfile
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from django.test import TestCase
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import Point, Polygon

from geo_processing.validation import metrics
from geo_processing.validation import rosleshoz as rl


class DetectionMetricsTest(TestCase):
    def _make(self):
        # GT: fire A at (100, 52) on 10.07, fire B at (101, 53) on 15.07
        gt = gpd.GeoDataFrame(
            {'fire_start_date': pd.to_datetime(['2024-07-10', '2024-07-15'])},
            geometry=[Point(100.0, 52.0), Point(101.0, 53.0)],
            crs='EPSG:4326',
        )
        # Strela: one point ~110 m from GT A (TP), one far away (FP); GT B -> FN
        strela = gpd.GeoDataFrame(
            {'detected_at': pd.to_datetime(['2024-07-10 12:00', '2024-07-20 12:00'])},
            geometry=[Point(100.001, 52.001), Point(105.0, 55.0)],
            crs='EPSG:4326',
        )
        return gt, strela

    def test_tp_fp_fn_and_prf1(self):
        gt, strela = self._make()
        res = metrics.validate_detection(gt, strela)
        self.assertEqual(res['tp'], 1)
        self.assertEqual(res['fp'], 1)
        self.assertEqual(res['fn'], 1)
        self.assertAlmostEqual(res['precision'], 0.5)
        self.assertAlmostEqual(res['recall'], 0.5)
        self.assertAlmostEqual(res['f1'], 0.5)
        self.assertEqual(res['strela_total'], 2)
        self.assertEqual(res['gt_total'], 2)

    def test_time_window_rejects_mismatch(self):
        # Spatially close but 30 days apart -> no match (window = +/-20 d)
        gt = gpd.GeoDataFrame(
            {'fire_start_date': pd.to_datetime(['2024-07-10'])},
            geometry=[Point(100.0, 52.0)], crs='EPSG:4326')
        strela = gpd.GeoDataFrame(
            {'detected_at': pd.to_datetime(['2024-08-09'])},
            geometry=[Point(100.001, 52.001)], crs='EPSG:4326')
        res = metrics.validate_detection(gt, strela)
        self.assertEqual(res['tp'], 0)
        self.assertEqual(res['fp'], 1)
        self.assertEqual(res['fn'], 1)

    def test_many_to_one_matches_use_real_indices(self):
        # Regression (geopandas >= 1.0 renames sjoin index columns): with more
        # join rows than GT fires, indices must map to real rows, not
        # positions. Four Strela points all inside GT B's 500 m buffer.
        gt = gpd.GeoDataFrame(
            {'fire_start_date': pd.to_datetime(['2024-07-10', '2024-07-15'])},
            geometry=[Point(100.0, 52.0), Point(101.0, 53.0)], crs='EPSG:4326')
        strela = gpd.GeoDataFrame(
            {'detected_at': pd.to_datetime(['2024-07-15 12:00'] * 4)},
            geometry=[Point(101.0005, 53.0005), Point(101.001, 53.001),
                      Point(101.0015, 53.0005), Point(101.0005, 53.0015)],
            crs='EPSG:4326')
        res = metrics.validate_detection(gt, strela)
        self.assertEqual(res['tp'], 1)
        self.assertEqual(res['fp'], 3)
        self.assertEqual(res['fn'], 1)
        self.assertEqual(res['strela_total'], 4)
        self.assertEqual(res['gt_total'], 2)
        self.assertAlmostEqual(res['precision'], 1 / 4)
        self.assertAlmostEqual(res['recall'], 0.5)

    def test_geodesic_buffer_is_in_meters(self):
        pt = gpd.GeoDataFrame(geometry=[Point(100.0, 52.0)], crs='EPSG:4326')
        buf = metrics.geodesic_buffer(pt, 500)
        lat = 52.0
        expected_half_span_deg = 500 / (111320 * math.cos(math.radians(lat)))
        span_deg = buf.geometry.iloc[0].bounds[2] - buf.geometry.iloc[0].bounds[0]
        self.assertAlmostEqual(span_deg / 2, expected_half_span_deg,
                               delta=expected_half_span_deg * 0.15)


class DeduplicationTest(TestCase):
    def _gdf(self, lons, lats, times, frp=None):
        cols = {'detected_at': pd.to_datetime(times)}
        if frp is not None:
            cols['frp'] = frp
        return gpd.GeoDataFrame(
            cols, geometry=[Point(lo, la) for lo, la in zip(lons, lats)],
            crs='EPSG:4326')

    def test_close_in_space_and_time_merge_into_one_spot(self):
        # ~205 m apart, 1 day apart -> one spot
        g = self._gdf([100.0, 100.003], [52.0, 52.0],
                      ['2024-07-10 12:00', '2024-07-11 12:00'])
        out, info = metrics.deduplicate_detections(g)
        self.assertEqual(len(out), 1)
        self.assertEqual(info['raw'], 2)
        self.assertEqual(info['clusters'], 1)

    def test_time_window_prevents_merge(self):
        # ~205 m apart but 5 days apart -> two spots
        g = self._gdf([100.0, 100.003], [52.0, 52.0],
                      ['2024-07-10 12:00', '2024-07-15 12:00'])
        out, info = metrics.deduplicate_detections(g)
        self.assertEqual(info['clusters'], 2)

    def test_distance_prevents_merge(self):
        # ~820 m apart, same time -> two spots
        g = self._gdf([100.0, 100.012], [52.0, 52.0],
                      ['2024-07-10 12:00', '2024-07-10 12:00'])
        out, info = metrics.deduplicate_detections(g)
        self.assertEqual(info['clusters'], 2)

    def test_transitive_chain_is_one_spot(self):
        # A-B ~300 m, B-C ~300 m, A-C ~600 m, same day -> one spot
        g = self._gdf([100.0, 100.0044, 100.0088], [52.0, 52.0, 52.0],
                      ['2024-07-10 12:00'] * 3)
        out, info = metrics.deduplicate_detections(g)
        self.assertEqual(info['clusters'], 1)
        self.assertEqual(int(out['cluster_size'].iloc[0]), 3)

    def test_representative_is_earliest_and_cluster_stats(self):
        g = self._gdf([100.0, 100.001, 100.001], [52.0, 52.0, 52.0],
                      ['2024-07-10 06:00', '2024-07-11 06:00',
                       '2024-07-11 09:00'],
                      frp=[5.0, 20.0, 8.0])
        out, info = metrics.deduplicate_detections(g)
        self.assertEqual(len(out), 1)
        self.assertEqual(str(out['detected_at'].iloc[0]), '2024-07-10 06:00:00')
        self.assertEqual(int(out['cluster_size'].iloc[0]), 3)
        self.assertEqual(str(out['last_at'].iloc[0]), '2024-07-11 09:00:00')
        self.assertAlmostEqual(float(out['frp_max'].iloc[0]), 20.0)
        self.assertAlmostEqual(float(out['frp_mean'].iloc[0]), 11.0)
        self.assertAlmostEqual(float(out['days_active'].iloc[0]), 2.1,
                               delta=0.05)

    def test_not_applicable_returns_none_info(self):
        g = self._gdf([100.0], [52.0], ['2024-07-10 12:00'])
        out, info = metrics.deduplicate_detections(g)
        self.assertIsNone(info)
        self.assertEqual(len(out), 1)

    def test_activity_period_match_inside_span(self):
        # fire start 07-18, spot active 07-12..07-20: inside the activity
        # period -> TP even though the first detection is 6 days earlier
        # (matching against detected_at alone would give FN)
        gt = gpd.GeoDataFrame(
            {'fire_start_date': pd.to_datetime(['2024-07-18'])},
            geometry=[Point(100.0, 52.0)], crs='EPSG:4326')
        strela = gpd.GeoDataFrame(
            {'detected_at': pd.to_datetime(['2024-07-12']),
             'first_at': pd.to_datetime(['2024-07-12']),
             'last_at': pd.to_datetime(['2024-07-20'])},
            geometry=[Point(100.001, 52.001)], crs='EPSG:4326')
        res = metrics.validate_detection(gt, strela)
        self.assertEqual(res['tp'], 1)
        self.assertEqual(res['fn'], 0)

    def test_activity_period_outside_window_no_match(self):
        # fire start 07-20, spot active until 07-14 (gap 6 days > 3) -> FN
        gt = gpd.GeoDataFrame(
            {'fire_start_date': pd.to_datetime(['2024-07-20'])},
            geometry=[Point(100.0, 52.0)], crs='EPSG:4326')
        strela = gpd.GeoDataFrame(
            {'detected_at': pd.to_datetime(['2024-07-12']),
             'first_at': pd.to_datetime(['2024-07-12']),
             'last_at': pd.to_datetime(['2024-07-14'])},
            geometry=[Point(100.001, 52.001)], crs='EPSG:4326')
        res = metrics.validate_detection(gt, strela)
        self.assertEqual(res['tp'], 0)
        self.assertEqual(res['fn'], 1)


class ReportPromptTest(TestCase):
    """report: the prompt that triggered the run is printed at the top."""

    def _result(self, **extra):
        result = {
            'region_name': 'Иркутская область',
            'bbox': [96, 46.71, 125.96, 63.98],
            'start_date': '2024-07-01',
            'end_date': '2024-07-31',
            'generated_at': '2026-10-24T12:00:00',
            'source': 'auto',
            'gt_fires': 10,
            'strela_fires': 20,
            'strela_burn_polygons': 0,
            'detection': None,
            'burns': None,
            'fp_industrial': None,
            'warnings': [],
        }
        result.update(extra)
        return result

    def test_markdown_contains_request_prompt(self):
        from geo_processing.validation import reporting
        result = self._result(request_prompt=(
            'Запусти валидацию системы «Стрела» инструментом run_validation с параметрами:\n'
            '- bbox: [96, 46.71, 125.96, 63.98]\n'
            '- dedupe: true'))
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'validation_report.md'
            reporting._write_markdown(result, p)
            text = p.read_text(encoding='utf-8')
        self.assertIn('Промт, сформировавший отчёт:', text)
        self.assertIn('Запусти валидацию системы «Стрела»', text)
        # промт стоит после заголовка, до блока с параметрами
        self.assertLess(text.index('Промт, сформировавший отчёт'),
                        text.index('**Регион:**'))

    def test_markdown_without_prompt_has_no_block(self):
        from geo_processing.validation import reporting
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'validation_report.md'
            reporting._write_markdown(self._result(), p)
            text = p.read_text(encoding='utf-8')
        self.assertNotIn('Промт, сформировавший отчёт', text)


class BurnMetricsTest(TestCase):
    def _setup_grid(self):
        """100x100 px grid @500 m in EPSG:3576 centered on Irkutsk (52N, 104E)
        with two 20x20 burned blocks. The realistic latitude matters: near the
        pole (origin of EPSG:3576) a box does not round-trip WGS84 <-> 3576."""
        res = 500.0
        t = Transformer.from_crs('EPSG:4326', 'EPSG:3576', always_xy=True)
        cx, cy = t.transform(104.0, 52.0)
        x0 = math.floor((cx - 25000.0) / res) * res
        y1 = math.ceil((cy + 25000.0) / res) * res
        transform = from_origin(x0, y1, res, res)
        burn_days = np.zeros((100, 100), dtype=np.float32)
        valid = np.ones((100, 100), dtype=bool)
        day_a = datetime(2024, 7, 15).toordinal()
        day_b = datetime(2024, 7, 20).toordinal()
        burn_days[20:40, 20:40] = day_a    # block A
        burn_days[50:70, 50:70] = day_b    # block B
        return burn_days, valid, transform, res, x0, y1

    def _wgs_poly(self, x0m, x1m, y0m, y1m):
        t = Transformer.from_crs('EPSG:3576', 'EPSG:4326', always_xy=True)
        corners = [t.transform(x, y) for x, y in
                   [(x0m, y1m), (x1m, y1m), (x1m, y0m), (x0m, y0m), (x0m, y1m)]]
        return Polygon([(lon, lat) for lon, lat in corners])

    def _block_poly(self, x0, y1, row0, row1, col0, col1):
        """WGS84 polygon exactly covering grid rows [row0,row1), cols [col0,col1)."""
        return self._wgs_poly(
            x0 + col0 * 500, x0 + col1 * 500,
            y1 - row1 * 500, y1 - row0 * 500,
        )

    def test_iou_and_per_polygon_area(self):
        burn_days, valid, transform, res, x0, y1 = self._setup_grid()
        # Strela polygon covers block A exactly (20x20 px = 10x10 km = 10 000 ha;
        # a 500 m pixel is 25 ha). Subset of burned A+B.
        poly_a = self._block_poly(x0, y1, 20, 40, 20, 40)
        strela = gpd.GeoDataFrame(
            {'burn_date': pd.to_datetime(['2024-07-15']),
             'area_ha': [10000.0], 'severity': ['low']},
            geometry=[poly_a], crs='EPSG:4326')
        out = metrics.compute_burn_metrics(
            strela, burn_days, valid, transform,
            datetime(2024, 7, 1), datetime(2024, 7, 31))
        # Strela mask = block A (buffered 1 km: ~4 px per side), burned = A+B:
        # IoU ~ 400 / (800 + buffer ring), a bit below 0.5
        self.assertAlmostEqual(out['iou'], 0.5, delta=0.2)
        self.assertEqual(out['polygons_compared'], 1)
        self.assertAlmostEqual(out['modis_area_ha'], 10000.0, delta=300.0)
        self.assertAlmostEqual(out['temporal_lag_days'], 0.0, delta=0.5)
        # Single polygon -> correlation is undefined; F1 is defined
        self.assertIsNone(out['area_correlation'])
        self.assertGreater(out['burn_f1'], 0.5)
        self.assertLess(out['burn_f1'], 0.75)

    def test_area_correlation_with_three_polygons(self):
        burn_days, valid, transform, res, x0, y1 = self._setup_grid()
        polys = [
            self._block_poly(x0, y1, 20, 40, 20, 40),   # block A: modis ~10 000 ha
            self._block_poly(x0, y1, 50, 70, 50, 70),   # block B: modis ~10 000 ha
            self._block_poly(x0, y1, 20, 40, 20, 30),   # half of A: modis ~5 000 ha
        ]
        strela = gpd.GeoDataFrame(
            {'burn_date': pd.to_datetime(['2024-07-15', '2024-07-20', '2024-07-15']),
             'area_ha': [12000.0, 8000.0, 6000.0],
             'severity': ['low', 'moderate', 'high']},
            geometry=polys, crs='EPSG:4326')
        out = metrics.compute_burn_metrics(
            strela, burn_days, valid, transform,
            datetime(2024, 7, 1), datetime(2024, 7, 31))
        self.assertEqual(out['polygons_compared'], 3)
        self.assertIsNotNone(out['area_correlation'])
        self.assertIsNotNone(out['mean_area_deviation_pct'])


class RosleshozLoaderTest(TestCase):
    def test_loads_and_filters(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / 'extracted').mkdir()
            csv_path = td / 'extracted' / 'data_fires_1T_v_test.csv'
            with open(csv_path, 'w', encoding='utf-8', newline='') as f:
                w = csv.writer(f, delimiter=';')
                w.writerow(['region', 'latitude', 'longitude', 'date_beginning',
                            'date_end', 'area_total', 'area_forest', 'type',
                            'code', 'current_state', 'zone'])
                w.writerow(['Иркутская область', '52.0', '100.0', '05.07.2024',
                            '06.07.2024', '100', '50', 'Лесные', 'к-1',
                            'ликвидирован 06.07.2024', ''])
                w.writerow(['Иркутская область', '53.0', '101.0', '15.07.2024',
                            '16.07.2024', '50', '20', 'Лесные', 'к-2',
                            'ликвидирован 16.07.2024', ''])
                # Outside bbox:
                w.writerow(['Свердловская область', '56.8', '60.6', '10.07.2024',
                            '10.07.2024', '10', '5', 'Лесные', 'к-3',
                            'ликвидирован 10.07.2024', ''])

            gdf = rl.load_rosleshoz([99, 51, 102, 54], '2024-07-01', '2024-07-31',
                                    data_dir=td)
            self.assertEqual(len(gdf), 2)
            self.assertEqual(gdf.crs.to_epsg(), 4326)
            self.assertIn('fire_start_date', gdf.columns)
            self.assertAlmostEqual(float(gdf['area_ha'].iloc[0]), 100.0)

            # Cached slice is returned without re-reading the CSV
            gdf2 = rl.load_rosleshoz([99, 51, 102, 54], '2024-07-01', '2024-07-31',
                                     data_dir=td)
            self.assertEqual(len(gdf2), 2)

    def test_period_filter(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / 'extracted').mkdir()
            csv_path = td / 'extracted' / 'data_fires_1T_v_test.csv'
            with open(csv_path, 'w', encoding='utf-8', newline='') as f:
                w = csv.writer(f, delimiter=';')
                w.writerow(['region', 'latitude', 'longitude', 'date_beginning',
                            'date_end', 'area_total', 'area_forest', 'type',
                            'code', 'current_state', 'zone'])
                w.writerow(['Иркутская область', '52.0', '100.0', '05.06.2024',
                            '06.06.2024', '100', '50', 'Лесные', 'к-1',
                            'ликвидирован 06.06.2024', ''])
                w.writerow(['Иркутская область', '52.5', '100.5', '05.07.2024',
                            '06.07.2024', '100', '50', 'Лесные', 'к-2',
                            'ликвидирован 06.07.2024', ''])

            gdf = rl.load_rosleshoz([99, 51, 102, 54], '2024-07-01', '2024-07-31',
                                    data_dir=td)
            self.assertEqual(len(gdf), 1)
            self.assertEqual(str(gdf['fire_start_date'].iloc[0].date()), '2024-07-05')