import json
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view
from rest_framework.response import Response
from rest_framework_gis.filters import InBBOXFilter, DistanceToPointFilter
from django.contrib.gis.geos import Polygon, Point, GEOSGeometry
from django.contrib.gis.db.models.functions import Distance
from django.db import models
from django.views.decorators.csrf import csrf_exempt
from datetime import datetime, timedelta
import logging
from pyproj import Transformer
from shapely.ops import transform as shapely_transform

from .models import FireHotspot, BurnArea, ProcessingJob, BurnReport
from .serializers import (
    FireHotspotSerializer, BurnAreaSerializer, ProcessingJobSerializer,
    ChatMessageSerializer, FireStatsSerializer, ReportGenerateSerializer,
    BurnReportSerializer,
)
from geo_processing.chat_handler import handle_chat_message

logger = logging.getLogger(__name__)


class FireHotspotViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for fire hotspots.

    list: Return all fire hotspots as GeoJSON FeatureCollection.
    retrieve: Return a single fire hotspot.
    """
    queryset = FireHotspot.objects.all()
    serializer_class = FireHotspotSerializer
    filter_backends = [InBBOXFilter]
    bbox_filter_field = 'location'
    bbox_filter_include_overlapping = True

    def get_queryset(self):
        qs = super().get_queryset()

        # Filter by date range
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        if date_from:
            qs = qs.filter(detected_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(detected_at__date__lte=date_to)

        # Filter by minimum confidence
        min_confidence = self.request.query_params.get('min_confidence')
        if min_confidence:
            confidence_order = {'low': 0, 'nominal': 1, 'high': 2}
            min_level = confidence_order.get(min_confidence, 0)
            sources = [s for s, v in confidence_order.items() if v >= min_level]
            qs = qs.filter(confidence__in=sources)

        # Filter by minimum FRP
        min_frp = self.request.query_params.get('min_frp')
        if min_frp:
            qs = qs.filter(frp__gte=float(min_frp))

        return qs

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get fire statistics for the current filter set."""
        qs = self.get_queryset()

        stats = {
            'total_hotspots': qs.count(),
            'by_source': {},
            'by_confidence': {},
            'avg_brightness': 0,
            'avg_frp': 0,
        }

        for source, count in qs.values_list('source').annotate(
            count=models.Count('id')
        ).values_list('source', 'count'):
            stats['by_source'][source] = count

        for conf, count in qs.values_list('confidence').annotate(
            count=models.Count('id')
        ).values_list('confidence', 'count'):
            stats['by_confidence'][conf] = count

        agg = qs.aggregate(
            avg_brightness=models.Avg('brightness'),
            avg_frp=models.Avg('frp'),
        )
        stats['avg_brightness'] = round(agg['avg_brightness'] or 0, 1)
        stats['avg_frp'] = round(agg['avg_frp'] or 0, 2)

        return Response(stats)


class BurnAreaViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for burned areas.

    list: Return all burn areas as GeoJSON FeatureCollection.
    retrieve: Return a single burn area.
    """
    queryset = BurnArea.objects.all()
    serializer_class = BurnAreaSerializer
    filter_backends = [InBBOXFilter]
    bbox_filter_field = 'geometry'
    bbox_filter_include_overlapping = True

    def get_queryset(self):
        qs = super().get_queryset()

        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        if date_from:
            qs = qs.filter(post_date__gte=date_from)
        if date_to:
            qs = qs.filter(post_date__lte=date_to)

        min_area = self.request.query_params.get('min_area_ha')
        if min_area:
            qs = qs.filter(area_ha__gte=float(min_area))

        return qs

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get burn area statistics."""
        qs = self.get_queryset()

        stats = {
            'total_areas': qs.count(),
            'total_area_ha': round(sum(b.area_ha for b in qs), 2),
            'by_severity': {},
            'avg_dnbr': 0,
        }

        for severity in dict(BurnArea.SEVERITY_CHOICES):
            severity_qs = qs.filter(severity=severity)
            if severity_qs.exists():
                stats['by_severity'][severity] = {
                    'count': severity_qs.count(),
                    'area_ha': round(sum(b.area_ha for b in severity_qs), 2),
                }

        agg = qs.aggregate(avg_dnbr=models.Avg('dnbr_mean'))
        stats['avg_dnbr'] = round(agg['avg_dnbr'] or 0, 3)

        return Response(stats)


class ProcessingJobViewSet(viewsets.ModelViewSet):
    """API endpoint for processing jobs."""
    queryset = ProcessingJob.objects.all()
    serializer_class = ProcessingJobSerializer

    def create(self, request, *args, **kwargs):
        """Create and start a new processing job."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        job = serializer.save(status='pending')

        # TODO: Trigger async processing here
        # For now, just return the job
        job.status = 'pending'
        job.save()

        return Response(
            ProcessingJobSerializer(job).data,
            status=status.HTTP_201_CREATED,
        )


@csrf_exempt
@api_view(['POST'])
def chat_view(request):
    """
    AI chat endpoint. Accepts a message and optional bbox,
    returns AI response with optional map data.
    """
    serializer = ChatMessageSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    message = serializer.validated_data['message']
    bbox = serializer.validated_data.get('bbox')
    history = serializer.validated_data.get('history') or []

    result = handle_chat_message(
        message,
        bbox,
        history=[{'role': h['role'], 'content': h['content']} for h in history],
    )

    return Response(result)


@csrf_exempt
@api_view(['POST'])
def fetch_fires_view(request):
    """
    Fetch fresh fire data from NASA FIRMS and store in database.
    Returns the fetched fires as GeoJSON.
    """
    from geo_processing.firms_client import fetch_all_sources
    from geo_processing.fire_filter import filter_fire_hotspots
    from django.contrib.gis.geos import Point

    bbox_str = request.data.get('bbox')
    days = request.data.get('days', 1)
    source = request.data.get('source', 'all')

    bbox = None
    if bbox_str:
        try:
            if isinstance(bbox_str, list):
                bbox = tuple(float(x) for x in bbox_str)
            else:
                bbox = tuple(float(x) for x in bbox_str.split(','))
        except (ValueError, TypeError):
            return Response({'error': 'Invalid bbox format'}, status=400)

    # Fetch from FIRMS
    if source == 'all':
        fires = fetch_all_sources(bbox=bbox, days=days)
    else:
        from geo_processing.firms_client import fetch_active_fires
        fires = fetch_active_fires(source=source, bbox=bbox, days=days)

    # Filter
    valid, filtered = filter_fire_hotspots(fires)

    # Store in database
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

    # Return as GeoJSON
    features = []
    for fire in valid:
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [fire['longitude'], fire['latitude']],
            },
            'properties': {
                'brightness': fire['brightness'],
                'confidence': fire['confidence'],
                'frp': fire.get('frp'),
                'source': fire['source'],
                'detected_at': fire['detected_at'].isoformat(),
                'daynight': fire.get('daynight', 'D'),
            },
        })

    return Response({
        'type': 'FeatureCollection',
        'features': features,
        'stats': {
            'fetched': len(fires),
            'valid': len(valid),
            'filtered': len(filtered),
            'created': created,
        }
    })


@csrf_exempt
@api_view(['POST'])
def map_burns_view(request):
    """
    Map burn severity using Sentinel-2 dNBR analysis.
    Accepts bbox, pre_date, post_date and returns burn area polygons.
    """
    from geo_processing.stac_client import search_sentinel2, get_sentinel2_assets, get_scl_asset
    from geo_processing.burn_severity import process_sentinel2_burn
    from django.contrib.gis.geos import MultiPolygon, GEOSGeometry
    import numpy as np
    from rasterio.features import shapes
    from shapely.geometry import shape
    from shapely.ops import unary_union

    bbox = request.data.get('bbox')
    pre_date = request.data.get('pre_date')
    post_date = request.data.get('post_date')
    max_cloud_cover = request.data.get('max_cloud_cover', 30)

    print(f"[DEBUG] map_burns_view called with bbox={bbox}, pre_date={pre_date}, post_date={post_date}")

    if not all([bbox, pre_date, post_date]):
        return Response(
            {'error': 'Missing required parameters: bbox, pre_date, post_date'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        if isinstance(bbox, str):
            bbox = tuple(float(x) for x in bbox.split(','))
        elif isinstance(bbox, list):
            bbox = tuple(float(x) for x in bbox)
        else:
            raise ValueError('Invalid bbox format')
    except (ValueError, TypeError) as e:
        return Response(
            {'error': f'Invalid bbox format: {e}'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Validate bbox size to prevent OOM
    lon_span = bbox[2] - bbox[0]
    lat_span = bbox[3] - bbox[1]
    max_span = 30  # Maximum 30 degrees (~3000km × 3000km)

    if lon_span > max_span or lat_span > max_span:
        return Response(
            {'error': f'Bbox too large ({lon_span:.1f}° × {lat_span:.1f}°). Maximum size is {max_span}° × {max_span}°. Please specify a smaller region.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        # Search for pre-fire Sentinel-2 scenes (search in range: pre_date - 15 days to pre_date)
        from datetime import timedelta
        pre_date_obj = datetime.strptime(pre_date, '%Y-%m-%d').date()
        pre_date_start = (pre_date_obj - timedelta(days=15)).isoformat()
        
        print(f"[DEBUG] Searching for pre-fire scenes ({pre_date_start} to {pre_date})...")
        pre_scenes = search_sentinel2(
            bbox=bbox,
            start_date=pre_date_start,
            end_date=pre_date,
            max_cloud_cover=max_cloud_cover,
            limit=5,
        )

        if not pre_scenes:
            return Response(
                {'error': f'No pre-fire Sentinel-2 scenes found for {pre_date_start} to {pre_date}'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Select best pre-fire scene (lowest cloud cover)
        pre_scene = min(pre_scenes, key=lambda s: s.properties.get('eo:cloud_cover', 100))
        print(f"[DEBUG] Selected pre-fire scene: {pre_scene.id}")

        # Search for post-fire Sentinel-2 scenes (search in range: post_date to post_date + 15 days)
        post_date_obj = datetime.strptime(post_date, '%Y-%m-%d').date()
        post_date_end = (post_date_obj + timedelta(days=15)).isoformat()
        
        print(f"[DEBUG] Searching for post-fire scenes ({post_date} to {post_date_end})...")
        post_scenes = search_sentinel2(
            bbox=bbox,
            start_date=post_date,
            end_date=post_date_end,
            max_cloud_cover=max_cloud_cover,
            limit=5,
        )

        if not post_scenes:
            return Response(
                {'error': f'No post-fire Sentinel-2 scenes found for {post_date} to {post_date_end}'},
                status=status.HTTP_404_NOT_FOUND
            )

        # Select best post-fire scene (lowest cloud cover)
        post_scene = min(post_scenes, key=lambda s: s.properties.get('eo:cloud_cover', 100))
        print(f"[DEBUG] Selected post-fire scene: {post_scene.id}")

        # Get signed asset URLs
        print(f"[DEBUG] Getting signed asset URLs...")
        pre_assets = get_sentinel2_assets(pre_scene, bands=['B08', 'B12'])
        post_assets = get_sentinel2_assets(post_scene, bands=['B08', 'B12'])
        print(f"[DEBUG] Pre assets: {list(pre_assets.keys())}")
        print(f"[DEBUG] Post assets: {list(post_assets.keys())}")

        # Get SCL assets for cloud masking
        pre_scl_url = get_scl_asset(pre_scene)
        post_scl_url = get_scl_asset(post_scene)
        print(f"[DEBUG] Cloud masking: pre_scl={'available' if pre_scl_url else 'not available'}, post_scl={'available' if post_scl_url else 'not available'}")

        # Process burn severity with cloud masking
        print(f"[DEBUG] Processing burn severity...")
        result = process_sentinel2_burn(
            pre_assets=pre_assets,
            post_assets=post_assets,
            bbox=bbox,
            n_classes=4,
            pre_scl_url=pre_scl_url,
            post_scl_url=post_scl_url,
            apply_cloud_masking=True,
        )

        # Extract statistics
        stats = result['stats']
        severity_data = result['severity']
        transform = result['transform']
        source_crs = result['crs']

        print(f"[DEBUG] Burn severity stats: {stats}")
        print(f"[DEBUG] Severity data shape: {severity_data.shape if hasattr(severity_data, 'shape') else 'N/A'}")
        print(f"[DEBUG] Source CRS: {source_crs}")

        # Create transformer for reprojection to WGS84
        transformer = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
        project = lambda x, y: transformer.transform(x, y)

        # Create burn area polygons for each severity class
        features = []
        burn_areas_created = 0

        # Optimization parameters
        min_area_ha = 1.0  # Minimum polygon area in hectares
        simplify_tolerance = 0.005  # Simplification tolerance in degrees (~250-500m)
        downsample_factor = 4  # Reduce resolution by 4x for faster vectorization

        # Calculate approximate area conversion factor (degrees² to hectares)
        # At latitude ~60°: 1° lat ≈ 111km, 1° lon ≈ 55.5km
        # 1°² ≈ 6160 km² ≈ 616000 ha
        avg_lat = (bbox[1] + bbox[3]) / 2
        lat_rad = np.radians(avg_lat)
        deg_to_m_lat = 111000  # meters per degree latitude
        deg_to_m_lon = 111000 * np.cos(lat_rad)  # meters per degree longitude
        deg2_to_ha = (deg_to_m_lat * deg_to_m_lon) / 10000  # degrees² to hectares

        for severity_class in ['low', 'moderate', 'high']:
            if severity_class not in stats or stats[severity_class]['area_ha'] == 0:
                continue

            logger.info(f"Processing severity class: {severity_class}, area: {stats[severity_class]['area_ha']} ha")

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
                if value == 1:
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
                logger.info(f"No polygons created for {severity_class}")
                continue

            logger.info(f"Created {len(polygons)} polygons for {severity_class} (after filtering)")

            # Merge polygons
            merged = unary_union(polygons)

            # Simplify geometry to reduce complexity
            merged = merged.simplify(simplify_tolerance, preserve_topology=True)
            logger.info(f"Simplified geometry: {merged.geom_type}, vertices reduced")

            # Reproject from UTM to WGS84
            merged_wgs84 = shapely_transform(project, merged)
            logger.info(f"Reprojected to WGS84")

            # Convert to GeoJSON
            if merged_wgs84.geom_type == 'Polygon':
                geom_json = merged_wgs84.__geo_interface__
            elif merged_wgs84.geom_type == 'MultiPolygon':
                geom_json = merged_wgs84.__geo_interface__
            else:
                continue

            # Create BurnArea record
            try:
                geos_geom = GEOSGeometry(str(merged_wgs84), srid=4326)
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
                logger.info(f"Created BurnArea record: {burn_area.id}")
            except Exception as e:
                logger.error(f'Failed to save burn area: {e}')
                continue

            # Add to GeoJSON features
            features.append({
                'type': 'Feature',
                'geometry': geom_json,
                'properties': {
                    'severity': severity_class,
                    'area_ha': stats[severity_class]['area_ha'],
                    'mean_dnbr': stats[severity_class]['mean_dnbr'],
                    'pre_date': pre_date,
                    'post_date': post_date,
                },
            })

        return Response({
            'type': 'FeatureCollection',
            'features': features,
            'stats': {
                'total_burned_ha': stats['total_burned_ha'],
                'burn_areas_created': burn_areas_created,
                'pre_scene': pre_scene.id,
                'post_scene': post_scene.id,
                'by_severity': {
                    k: {'area_ha': v['area_ha'], 'mean_dnbr': v['mean_dnbr']}
                    for k, v in stats.items()
                    if k != 'total_burned_ha'
                },
            }
        })

    except Exception as e:
        logger.exception('Burn mapping failed')
        return Response(
            {'error': f'Burn mapping failed: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def overview_stats(request):
    """Get combined fire and burn statistics."""
    bbox_str = request.query_params.get('bbox')
    date_from = request.query_params.get('date_from')
    date_to = request.query_params.get('date_to')

    fire_qs = FireHotspot.objects.all()
    burn_qs = BurnArea.objects.all()

    if bbox_str:
        try:
            coords = [float(x) for x in bbox_str.split(',')]
            bbox_geom = Polygon.from_bbox(coords)
            fire_qs = fire_qs.filter(location__within=bbox_geom)
            burn_qs = burn_qs.filter(geometry__intersects=bbox_geom)
        except (ValueError, TypeError):
            pass

    if date_from:
        fire_qs = fire_qs.filter(detected_at__date__gte=date_from)
        burn_qs = burn_qs.filter(post_date__gte=date_from)
    if date_to:
        fire_qs = fire_qs.filter(detected_at__date__lte=date_to)
        burn_qs = burn_qs.filter(post_date__lte=date_to)

    stats = {
        'fires': {
            'total': fire_qs.count(),
            'high_confidence': fire_qs.filter(confidence='high').count(),
            'avg_brightness': round(
                fire_qs.aggregate(models.Avg('brightness'))['brightness__avg'] or 0, 1
            ),
        },
        'burns': {
            'total_areas': burn_qs.count(),
            'total_area_ha': round(sum(b.area_ha for b in burn_qs), 2),
        },
    }

    return Response(stats)


class BurnReportViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint for burn reports.

    list: Return all burn reports.
    retrieve: Return a single burn report.
    """
    queryset = BurnReport.objects.all()
    serializer_class = BurnReportSerializer
    lookup_field = 'report_id'

    @action(detail=False, methods=['post'])
    def generate(self, request):
        """Generate a new burn report."""
        from geo_processing.report_generator import BurnReportGenerator

        serializer = ReportGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        bbox = tuple(serializer.validated_data['bbox'])
        date_from = serializer.validated_data['date_from']
        date_to = serializer.validated_data['date_to']
        include_landcover = serializer.validated_data.get('include_landcover', True)

        try:
            generator = BurnReportGenerator()
            report_data = generator.generate_report(
                bbox=bbox,
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                include_landcover=include_landcover,
            )

            if 'error' in report_data:
                return Response(
                    {'error': report_data['error']},
                    status=status.HTTP_404_NOT_FOUND
                )

            return Response(report_data, status=status.HTTP_201_CREATED)

        except Exception as e:
            logger.exception('Report generation failed')
            return Response(
                {'error': f'Report generation failed: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['get'])
    def export_geojson(self, request, report_id=None):
        """Export burn areas for a report as GeoJSON."""
        report = self.get_object()
        burn_areas = report.burn_areas.all()

        features = []
        for burn_area in burn_areas:
            features.append({
                'type': 'Feature',
                'geometry': json.loads(burn_area.geometry.geojson),
                'properties': {
                    'severity': burn_area.severity,
                    'area_ha': burn_area.area_ha,
                    'mean_dnbr': burn_area.dnbr_mean,
                    'pre_date': str(burn_area.pre_date),
                    'post_date': str(burn_area.post_date),
                },
            })

        return Response({
            'type': 'FeatureCollection',
            'features': features,
        })

    @action(detail=True, methods=['get'])
    def export_csv(self, request, report_id=None):
        """Export burn areas for a report as CSV."""
        import csv
        from django.http import HttpResponse

        report = self.get_object()
        burn_areas = report.burn_areas.all()

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="burn_report_{report_id}.csv"'

        writer = csv.writer(response)
        writer.writerow([
            'id', 'severity', 'area_ha', 'mean_dnbr',
            'pre_date', 'post_date', 'satellite', 'tile_id'
        ])

        for burn_area in burn_areas:
            writer.writerow([
                burn_area.id,
                burn_area.severity,
                burn_area.area_ha,
                burn_area.dnbr_mean,
                burn_area.pre_date,
                burn_area.post_date,
                burn_area.satellite,
                burn_area.tile_id,
            ])

        return response
