"""
Management command to map burn severity using Sentinel-2 dNBR analysis (multi-tile).

Usage:
    python manage.py map_burns --bbox 80,55,110,70 --pre_date 2025-06-01 --post_date 2025-07-15
    python manage.py map_burns --bbox 80,55,110,70 --pre_date 2025-06-01 --post_date 2025-07-15 --max_cloud_cover 20
    python manage.py map_burns --bbox 80,55,110,70 --pre_date 2025-06-01 --post_date 2025-07-15 --clear
"""

import logging
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError
from django.contrib.gis.geos import Polygon
from fires.models import BurnArea, ProcessingJob
from geo_processing.burn_mapping import map_burns_multitile

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Map burn severity using Sentinel-2 dNBR analysis (all tiles covering the bbox)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--bbox',
            type=str,
            required=True,
            help='Bounding box: minx,miny,maxx,maxy (e.g., 80,55,110,70 for Siberia)',
        )
        parser.add_argument(
            '--pre_date',
            type=str,
            required=True,
            help='Pre-fire date (YYYY-MM-DD) - before the fire',
        )
        parser.add_argument(
            '--post_date',
            type=str,
            required=True,
            help='Post-fire date (YYYY-MM-DD) - after the fire',
        )
        parser.add_argument(
            '--max_cloud_cover',
            type=int,
            default=30,
            help='Maximum cloud cover percentage (default: 30)',
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Delete existing BurnArea records for this bbox and post_date before mapping',
        )

    def handle(self, *args, **options):
        # Parse bbox
        try:
            bbox = tuple(float(x) for x in options['bbox'].split(','))
            if len(bbox) != 4:
                raise ValueError
        except ValueError:
            raise CommandError('Invalid bbox format. Use: minx,miny,maxx,maxy')

        pre_date = options['pre_date']
        post_date = options['post_date']
        max_cloud_cover = options['max_cloud_cover']

        self.stdout.write(f'Mapping burn severity for bbox: {bbox}')
        self.stdout.write(f'Pre-fire date: {pre_date}')
        self.stdout.write(f'Post-fire date: {post_date}')
        self.stdout.write(f'Max cloud cover: {max_cloud_cover}%')

        # Create processing job
        job = ProcessingJob.objects.create(
            job_type='burn_mapping',
            status='running',
            bbox=Polygon.from_bbox(bbox),
            params={
                'pre_date': pre_date,
                'post_date': post_date,
                'max_cloud_cover': max_cloud_cover,
            },
            started_at=datetime.now(),
        )

        try:
            if options['clear']:
                deleted, _ = BurnArea.objects.filter(
                    geometry__intersects=Polygon.from_bbox(bbox),
                    post_date=post_date,
                ).delete()
                self.stdout.write(f'Cleared {deleted} existing burn area records (bbox, post_date={post_date})')

            result = map_burns_multitile(
                bbox=bbox,
                pre_date=pre_date,
                post_date=post_date,
                max_cloud_cover=max_cloud_cover,
                job=job,  # progress + final status are tracked inside
            )
            summary = result['summary']

            if 'error' in summary:
                raise CommandError(summary['error'])

            parts = [f"Tiles: {summary['tiles_processed']}/{summary['tiles_total']} processed"]
            if summary['tiles_failed']:
                parts.append(f"{summary['tiles_failed']} failed")
            if summary['tiles_skipped']:
                parts.append(f"{len(summary['tiles_skipped'])} skipped (BURN_MAX_TILES limit)")
            self.stdout.write(self.style.SUCCESS(', '.join(parts)))

            for detail in summary['tile_details']:
                if detail.get('error'):
                    self.stdout.write(f"  {detail['tile']}: FAILED - {detail['error'][:200]}")
                else:
                    self.stdout.write(
                        f"  {detail['tile']}: burned {detail.get('burned_ha', 0)} ha, "
                        f"{detail.get('polygons', 0)} polygons "
                        f"(cloud pre {detail['pre_cloud_cover']}% / post {detail['post_cloud_cover']}%)"
                    )

            self.stdout.write(self.style.SUCCESS(
                f"Total burned area: {summary['total_burned_ha']} ha, "
                f"polygons: {summary['polygons_loaded']}"
            ))

        except Exception as e:
            # map_burns_multitile already marks the job failed for mapping
            # errors; cover errors outside the function (e.g. --clear).
            if job.status != 'failed':
                job.status = 'failed'
                job.error_message = str(e)
                job.completed_at = datetime.now()
                job.save()
            raise CommandError(f'Burn mapping failed: {e}')