"""
Tests for burn mapping job protections:

- sweep_zombie_jobs: finalize jobs stuck in 'running' (backend OOM/restart)
- duplicate-run guard: map_burns_multitile refuses to start a second
  concurrent burn mapping (two concurrent runs OOM-kill the backend)
"""

from datetime import timedelta
from unittest import mock

from django.contrib.gis.geos import Polygon
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from fires.models import ProcessingJob
from geo_processing.burn_mapping import (
    find_zombie_jobs,
    map_burns_multitile,
    sweep_zombie_jobs,
)

BBOX = (80, 50, 90, 60)
PRE_DATE = '2026-07-15'
POST_DATE = '2026-10-15'

EMPTY_RESULT = {
    'geojson': {'type': 'FeatureCollection', 'features': []},
    'data_type': 'burn',
    'summary': {'tiles_total': 0, 'tiles_processed': 0, 'tiles_failed': 0,
                'tiles_skipped': [], 'tile_details': [], 'total_burned_ha': 0,
                'by_severity': {}, 'polygons_loaded': 0},
}


def make_job(job_type='burn_mapping', status='running', minutes_ago=0, params=None):
    return ProcessingJob.objects.create(
        job_type=job_type,
        status=status,
        bbox=Polygon.from_bbox(BBOX),
        params=params or {'pre_date': PRE_DATE, 'post_date': POST_DATE, 'max_cloud_cover': 30},
        started_at=timezone.now() - timedelta(minutes=minutes_ago),
    )


class SweepZombieJobsTests(TestCase):
    def test_sweeps_old_running_jobs(self):
        job = make_job(minutes_ago=130)
        self.assertEqual(sweep_zombie_jobs(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertIsNotNone(job.completed_at)
        self.assertIn('stuck in "running"', job.error_message)

    def test_keeps_recent_running_jobs(self):
        job = make_job(minutes_ago=5)
        self.assertEqual(sweep_zombie_jobs(), 0)
        job.refresh_from_db()
        self.assertEqual(job.status, 'running')

    def test_respects_custom_timeout(self):
        job = make_job(minutes_ago=90)
        # 90 min > 60 min timeout -> zombie
        self.assertEqual(sweep_zombie_jobs(timeout_minutes=60), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')

    def test_job_type_filter(self):
        make_job(job_type='fire_detection', minutes_ago=130)
        make_job(job_type='burn_mapping', minutes_ago=5)
        self.assertEqual(sweep_zombie_jobs(job_type='burn_mapping'), 0)
        self.assertEqual(sweep_zombie_jobs(job_type='fire_detection'), 1)

    def test_does_not_touch_non_running_jobs(self):
        make_job(status='failed', minutes_ago=130)
        make_job(status='completed', minutes_ago=130)
        self.assertEqual(sweep_zombie_jobs(), 0)

    def test_find_zombie_jobs_returns_queryset(self):
        make_job(minutes_ago=130)
        make_job(minutes_ago=5)
        self.assertEqual(find_zombie_jobs().count(), 1)


class DuplicateRunGuardTests(TestCase):
    def test_blocks_when_active_burn_job(self):
        make_job(minutes_ago=5)
        with self.assertRaises(ValueError) as ctx:
            map_burns_multitile(bbox=BBOX, pre_date=PRE_DATE, post_date=POST_DATE)
        self.assertIn('already running', str(ctx.exception))
        # No extra job record was created
        self.assertEqual(ProcessingJob.objects.count(), 1)

    def test_allows_when_no_active_job(self):
        make_job(status='completed', minutes_ago=5)
        with mock.patch('geo_processing.burn_mapping.search_sentinel2', return_value=[]), \
             mock.patch('geo_processing.burn_mapping._map_burns_multitile_core',
                        return_value=EMPTY_RESULT) as core:
            map_burns_multitile(bbox=BBOX, pre_date=PRE_DATE, post_date=POST_DATE)
        core.assert_called_once()

    def test_allows_when_only_stale_burn_job(self):
        # Zombie (older than JOB_TIMEOUT_MINUTES) does not block new runs
        make_job(minutes_ago=130)
        with mock.patch('geo_processing.burn_mapping.search_sentinel2', return_value=[]), \
             mock.patch('geo_processing.burn_mapping._map_burns_multitile_core',
                        return_value=EMPTY_RESULT) as core:
            map_burns_multitile(bbox=BBOX, pre_date=PRE_DATE, post_date=POST_DATE)
        core.assert_called_once()

    def test_explicit_job_is_exempt(self):
        active = make_job(minutes_ago=5)
        with mock.patch('geo_processing.burn_mapping.search_sentinel2', return_value=[]), \
             mock.patch('geo_processing.burn_mapping._map_burns_multitile_core',
                        return_value=EMPTY_RESULT) as core:
            map_burns_multitile(bbox=BBOX, pre_date=PRE_DATE, post_date=POST_DATE, job=active)
        core.assert_called_once()


class MapBurnsAPIDuplicateTests(TestCase):
    def test_duplicate_run_returns_400(self):
        make_job(minutes_ago=5)
        resp = APIClient().post('/api/map-burns/', {
            'bbox': BBOX,
            'pre_date': PRE_DATE,
            'post_date': POST_DATE,
            'max_cloud_cover': 30,
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('already running', resp.data['error'])
