"""
Management command to fetch and store fire data from NASA FIRMS.

Usage:
    python manage.py fetch_fires --bbox 80,55,110,70 --days 1
    python manage.py fetch_fires --source VIIRS_SNPP --days 3
"""

from django.core.management.base import BaseCommand
from django.contrib.gis.geos import Point
from fires.models import FireHotspot
from geo_processing.firms_client import fetch_active_fires, fetch_all_sources
from geo_processing.fire_filter import filter_fire_hotspots


class Command(BaseCommand):
    help = 'Fetch fire hotspots from NASA FIRMS API'

    def add_arguments(self, parser):
        parser.add_argument(
            '--bbox',
            type=str,
            help='Bounding box: minx,miny,maxx,maxy (e.g., 80,55,110,70 for Siberia)',
        )
        parser.add_argument(
            '--source',
            type=str,
            default='all',
            choices=['all', 'VIIRS_SNPP', 'VIIRS_NOAA20', 'MODIS_Aqua', 'MODIS_Terra'],
            help='Satellite source (default: all)',
        )
        parser.add_argument(
            '--days',
            type=int,
            default=1,
            choices=[1, 10],
            help='Number of days to look back (default: 1)',
        )
        parser.add_argument(
            '--no-filter',
            action='store_true',
            help='Skip false positive filtering',
        )

    def handle(self, *args, **options):
        bbox = None
        if options['bbox']:
            try:
                bbox = tuple(float(x) for x in options['bbox'].split(','))
                if len(bbox) != 4:
                    raise ValueError
            except ValueError:
                self.stderr.write('Invalid bbox format. Use: minx,miny,maxx,maxy')
                return

        source = options['source']
        days = options['days']
        apply_filter = not options['no_filter']

        self.stdout.write(f'Fetching fires: source={source}, days={days}, bbox={bbox}')

        if source == 'all':
            fires = fetch_all_sources(bbox=bbox, days=days)
        else:
            fires = fetch_active_fires(source=source, bbox=bbox, days=days)

        self.stdout.write(f'Fetched {len(fires)} raw fire hotspots')

        if apply_filter:
            valid, filtered = filter_fire_hotspots(fires)
            self.stdout.write(
                f'After filtering: {len(valid)} valid, {len(filtered)} filtered out'
            )
        else:
            valid = fires
            for f in valid:
                f['filter_flag'] = ''

        created = 0
        for fire in valid:
            _, was_created = FireHotspot.objects.update_or_create(
                location=Point(fire['longitude'], fire['latitude']),
                detected_at=fire['detected_at'],
                source=fire['source'],
                defaults={
                    'brightness': fire['brightness'],
                    'brightness_31': fire.get('brightness_31'),
                    'confidence': fire['confidence'],
                    'frp': fire.get('frp'),
                    'satellite': fire.get('satellite', ''),
                    'instrument': fire.get('instrument', ''),
                    'daynight': fire.get('daynight', 'D'),
                    'filter_flag': fire.get('filter_flag', ''),
                },
            )
            if was_created:
                created += 1

        self.stdout.write(self.style.SUCCESS(f'Created/updated {created} fire hotspots'))
