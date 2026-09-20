"""
Management command to map burn severity using Sentinel-2 dNBR analysis.

Usage:
    python manage.py map_burns --bbox 80,55,110,70 --pre_date 2025-06-01 --post_date 2025-07-15
    python manage.py map_burns --bbox 80,55,110,70 --pre_date 2025-06-01 --post_date 2025-07-15 --max_cloud_cover 20
"""

import logging
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError
from django.contrib.gis.geos import MultiPolygon, Polygon, GEOSGeometry
from fires.models import BurnArea, ProcessingJob
from geo_processing.stac_client import search_sentinel2, get_sentinel2_assets
from geo_processing.burn_severity import process_sentinel2_burn
import rasterio
from rasterio.features import shapes
from shapely.geometry import shape
from shapely.ops import unary_union, transform as shapely_transform
from pyproj import Transformer
import numpy as np

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Map burn severity using Sentinel-2 dNBR analysis'

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
            # Search for pre-fire Sentinel-2 scenes (search in range: pre_date - 15 days to pre_date)
            from datetime import timedelta
            pre_date_obj = datetime.strptime(pre_date, '%Y-%m-%d').date()
            pre_date_start = (pre_date_obj - timedelta(days=15)).isoformat()
            
            self.stdout.write(f'Searching for pre-fire Sentinel-2 scenes ({pre_date_start} to {pre_date})...')
            pre_scenes = search_sentinel2(
                bbox=bbox,
                start_date=pre_date_start,
                end_date=pre_date,
                max_cloud_cover=max_cloud_cover,
                limit=5,
            )

            if not pre_scenes:
                raise CommandError(f'No pre-fire Sentinel-2 scenes found for {pre_date_start} to {pre_date}')

            # Select best pre-fire scene (lowest cloud cover)
            pre_scene = min(pre_scenes, key=lambda s: s.properties.get('eo:cloud_cover', 100))
            self.stdout.write(f'Selected pre-fire scene: {pre_scene.id} (cloud: {pre_scene.properties.get("eo:cloud_cover", "N/A")}%)')

            # Search for post-fire Sentinel-2 scenes (search in range: post_date to post_date + 15 days)
            post_date_obj = datetime.strptime(post_date, '%Y-%m-%d').date()
            post_date_end = (post_date_obj + timedelta(days=15)).isoformat()
            
            self.stdout.write(f'Searching for post-fire Sentinel-2 scenes ({post_date} to {post_date_end})...')
            post_scenes = search_sentinel2(
                bbox=bbox,
                start_date=post_date,
                end_date=post_date_end,
                max_cloud_cover=max_cloud_cover,
                limit=5,
            )

            if not post_scenes:
                raise CommandError(f'No post-fire Sentinel-2 scenes found for {post_date} to {post_date_end}')

            # Select best post-fire scene (lowest cloud cover)
            post_scene = min(post_scenes, key=lambda s: s.properties.get('eo:cloud_cover', 100))
            self.stdout.write(f'Selected post-fire scene: {post_scene.id} (cloud: {post_scene.properties.get("eo:cloud_cover", "N/A")}%)')

            # Get signed asset URLs
            self.stdout.write('Getting signed asset URLs...')
            pre_assets = get_sentinel2_assets(pre_scene, bands=['B08', 'B12'])
            post_assets = get_sentinel2_assets(post_scene, bands=['B08', 'B12'])

            # Process burn severity
            self.stdout.write('Calculating burn severity (dNBR)...')
            result = process_sentinel2_burn(
                pre_assets=pre_assets,
                post_assets=post_assets,
                bbox=bbox,
                n_classes=4,
            )

            # Extract statistics
            stats = result['stats']
            severity_data = result['severity']
            dnbr_data = result['dnbr']
            transform = result['transform']
            crs = result['crs']

            self.stdout.write(self.style.SUCCESS('Burn severity calculation complete!'))
            self.stdout.write(f'Total burned area: {stats["total_burned_ha"]:.2f} ha')

            # Create transformer for reprojection to WGS84
            transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
            project = lambda x, y: transformer.transform(x, y)

            # Create burn area polygons for each severity class
            burn_areas_created = 0

            # Optimization parameters
            min_area_ha = 1.0  # Minimum polygon area in hectares
            simplify_tolerance = 0.005  # Simplification tolerance in degrees (~250-500m)
            downsample_factor = 4  # Reduce resolution by 4x for faster vectorization

            # Calculate approximate area conversion factor (degrees² to hectares)
            avg_lat = (bbox[1] + bbox[3]) / 2
            lat_rad = np.radians(avg_lat)
            deg_to_m_lat = 111000  # meters per degree latitude
            deg_to_m_lon = 111000 * np.cos(lat_rad)  # meters per degree longitude
            deg2_to_ha = (deg_to_m_lat * deg_to_m_lon) / 10000  # degrees² to hectares

            for severity_class in ['low', 'moderate', 'high']:
                if severity_class not in stats or stats[severity_class]['area_ha'] == 0:
                    continue

                # Create mask for this severity class
                mask = (severity_data == severity_class).astype(np.uint8)

                # Downsample mask for faster vectorization (simple stride approach)
                if downsample_factor > 1:
                    mask_downsampled = mask[::downsample_factor, ::downsample_factor]
                    # Adjust transform for downsampled raster
                    downsampled_transform = transform * transform.scale(downsample_factor, downsample_factor)
                else:
                    mask_downsampled = mask
                    downsampled_transform = transform

                # Vectorize raster to polygons with filtering
                polygons = []
                for geom, value in shapes(mask_downsampled.astype(np.uint8), transform=downsampled_transform):
                    if value == 1:  # Only include pixels of this severity
                        try:
                            poly = shape(geom)
                            if poly.is_valid and poly.area > 0:
                                # Filter by area (convert degrees² to hectares)
                                area_ha = poly.area * deg2_to_ha
                                if area_ha >= min_area_ha:
                                    polygons.append(poly)
                        except Exception as e:
                            logger.warning(f'Invalid polygon: {e}')
                            continue

                if not polygons:
                    continue

                # Merge polygons
                merged = unary_union(polygons)

                # Simplify geometry to reduce complexity
                merged = merged.simplify(simplify_tolerance, preserve_topology=True)

                # Reproject from UTM to WGS84
                merged_wgs84 = shapely_transform(project, merged)

                # Convert to Django MultiPolygon
                if merged_wgs84.geom_type == 'Polygon':
                    merged_wgs84 = MultiPolygon([merged_wgs84])
                elif merged_wgs84.geom_type == 'MultiPolygon':
                    merged_wgs84 = MultiPolygon(merged_wgs84.geoms)
                else:
                    continue

                # Create Django geometry with SRID
                geos_geom = GEOSGeometry(merged_wgs84.wkt, srid=4326)

                # Create BurnArea record
                burn_area = BurnArea.objects.create(
                    geometry=geos_geom,
                    area_ha=stats[severity_class]['area_ha'],
                    severity=severity_class,
                    dnbr_mean=stats[severity_class]['mean_dnbr'],
                    pre_date=pre_date,
                    post_date=post_date,
                    satellite='Sentinel-2',
                    tile_id=pre_scene.id,
                )
                burn_areas_created += 1
                self.stdout.write(f'Created burn area: {severity_class} - {burn_area.area_ha:.2f} ha')

            # Update job status
            job.status = 'completed'
            job.completed_at = datetime.now()
            job.result_summary = {
                'total_burned_ha': stats['total_burned_ha'],
                'burn_areas_created': burn_areas_created,
                'pre_scene': pre_scene.id,
                'post_scene': post_scene.id,
                'stats': stats,
            }
            job.save()

            self.stdout.write(self.style.SUCCESS(
                f'Successfully created {burn_areas_created} burn area polygons'
            ))

        except Exception as e:
            job.status = 'failed'
            job.error_message = str(e)
            job.completed_at = datetime.now()
            job.save()
            raise CommandError(f'Burn mapping failed: {e}')
