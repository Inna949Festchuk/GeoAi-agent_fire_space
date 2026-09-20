"""
Management command to update industrial zones cache from Overture Maps.

Usage:
    python manage.py update_industrial_zones --bbox 80,55,110,70
    python manage.py update_industrial_zones --bbox 80,55,110,70 --clear
"""

from django.core.management.base import BaseCommand, CommandError
from django.contrib.gis.geos import Polygon
from fires.models import IndustrialZone
from geo_processing.overture_client import OvertureClient, cache_industrial_zones_to_db


class Command(BaseCommand):
    help = 'Update industrial zones cache from Overture Maps'

    def add_arguments(self, parser):
        parser.add_argument(
            '--bbox',
            type=str,
            required=True,
            help='Bounding box as "minx,miny,maxx,maxy" in WGS84',
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Clear existing zones in bbox before updating',
        )

    def handle(self, *args, **options):
        bbox_str = options['bbox']
        clear = options['clear']

        # Parse bbox
        try:
            coords = [float(x.strip()) for x in bbox_str.split(',')]
            if len(coords) != 4:
                raise ValueError('Bbox must have 4 coordinates')
            bbox = tuple(coords)
        except (ValueError, AttributeError) as e:
            raise CommandError(f'Invalid bbox format: {e}')

        # Validate bbox
        minx, miny, maxx, maxy = bbox
        if not (-180 <= minx <= 180 and -180 <= maxx <= 180):
            raise CommandError('Longitude must be between -180 and 180')
        if not (-90 <= miny <= 90 and -90 <= maxy <= 90):
            raise CommandError('Latitude must be between -90 and 90')
        if minx >= maxx or miny >= maxy:
            raise CommandError('Invalid bbox: min must be less than max')

        self.stdout.write(f'Updating industrial zones for bbox: {bbox}')

        # Clear existing zones if requested
        if clear:
            bbox_geom = Polygon.from_bbox(bbox)
            deleted_count, _ = IndustrialZone.objects.filter(
                geometry__intersects=bbox_geom
            ).delete()
            self.stdout.write(f'Cleared {deleted_count} existing zones')

        # Query and cache from Overture
        self.stdout.write('Querying Overture Maps...')
        try:
            cached_count = cache_industrial_zones_to_db(bbox)
            self.stdout.write(self.style.SUCCESS(
                f'Successfully cached {cached_count} industrial zones'
            ))
        except Exception as e:
            raise CommandError(f'Failed to update zones: {e}')

        # Show summary
        total_zones = IndustrialZone.objects.count()
        self.stdout.write(f'Total industrial zones in database: {total_zones}')
