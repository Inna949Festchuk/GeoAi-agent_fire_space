import json
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view
from rest_framework.response import Response
from rest_framework_gis.filters import InBBOXFilter, DistanceToPointFilter
from django.contrib.gis.geos import Polygon, Point, GEOSGeometry
from django.contrib.gis.db.models.functions import Distance
from django.db import models
from django.db.models import Q
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
        # Optimization: use detected_at__gte/lte with datetime instead of detected_at__date
        # This allows index scan instead of seq scan with DATE() function
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        if date_from:
            # Convert date to datetime for index-friendly query
            try:
                dt_from = datetime.strptime(date_from, '%Y-%m-%d')
                qs = qs.filter(detected_at__gte=dt_from)
            except ValueError:
                pass
        if date_to:
            try:
                # Include the entire end date by adding 1 day
                dt_to = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
                qs = qs.filter(detected_at__lt=dt_to)
            except ValueError:
                pass

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

        # Optimization: single query with GROUP BY for both source and confidence
        # Instead of separate queries for each grouping
        stats = {
            'total_hotspots': qs.count(),
            'by_source': {},
            'by_confidence': {},
            'avg_brightness': 0,
            'avg_frp': 0,
        }

        # Single aggregation query for all stats
        agg = qs.aggregate(
            total=models.Count('id'),
            avg_brightness=models.Avg('brightness'),
            avg_frp=models.Avg('frp'),
        )
        stats['avg_brightness'] = round(agg['avg_brightness'] or 0, 1)
        stats['avg_frp'] = round(agg['avg_frp'] or 0, 2)

        # Group by source — single query
        for row in qs.values('source').annotate(count=models.Count('id')):
            stats['by_source'][row['source']] = row['count']

        # Group by confidence — single query
        for row in qs.values('confidence').annotate(count=models.Count('id')):
            stats['by_confidence'][row['confidence']] = row['count']

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

        # Optimization: DB aggregation instead of loading all objects into Python
        # Single query with GROUP BY instead of ~9 separate queries
        agg = qs.aggregate(
            total_areas=models.Count('id'),
            total_area_ha=models.Sum('area_ha'),
            avg_dnbr=models.Avg('dnbr_mean'),
        )

        stats = {
            'total_areas': agg['total_areas'] or 0,
            'total_area_ha': round(agg['total_area_ha'] or 0, 2),
            'by_severity': {},
            'avg_dnbr': round(agg['avg_dnbr'] or 0, 3),
        }

        # Single GROUP BY query for severity breakdown
        for row in qs.values('severity').annotate(
            count=models.Count('id'),
            area_ha=models.Sum('area_ha'),
        ):
            stats['by_severity'][row['severity']] = {
                'count': row['count'],
                'area_ha': round(row['area_ha'] or 0, 2),
            }

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
    # Optimization: wrap in transaction.atomic for faster bulk inserts
    from django.db import transaction

    created = 0
    with transaction.atomic():
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
    Map burn severity using Sentinel-2 dNBR analysis (multi-tile).

    Processes all Sentinel-2 tiles covering the bbox (up to BURN_MAX_TILES)
    and returns burn area polygons with a per-tile coverage summary.
    """
    from geo_processing.burn_mapping import map_burns_multitile

    bbox = request.data.get('bbox')
    pre_date = request.data.get('pre_date')
    post_date = request.data.get('post_date')
    max_cloud_cover = request.data.get('max_cloud_cover', 30)

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
    max_span = 30  # Maximum 30 degrees (~3000km x 3000km)

    if lon_span > max_span or lat_span > max_span:
        return Response(
            {'error': f'Bbox too large ({lon_span:.1f}° x {lat_span:.1f}°). Maximum size is {max_span}° x {max_span}°. Please specify a smaller region.'},
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        result = map_burns_multitile(
            bbox=bbox,
            pre_date=pre_date,
            post_date=post_date,
            max_cloud_cover=max_cloud_cover,
        )
    except ValueError as e:
        return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.exception('Burn mapping failed')
        return Response(
            {'error': f'Burn mapping failed: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    summary = result['summary']
    if 'error' in summary:
        return Response({'error': summary['error']}, status=status.HTTP_404_NOT_FOUND)

    return Response({
        'type': 'FeatureCollection',
        'features': result['geojson']['features'],
        'stats': {
            **summary,
            'burn_areas_created': summary.get('polygons_loaded', 0),
        }
    })


@csrf_exempt
@api_view(['POST'])
def sentinel2_search_view(request):
    """
    Search for available Sentinel-2 scenes for a region and period.

    Returns the found scenes (id, datetime, cloud cover, MGRS tile) plus the
    clearest scene per tile. Used by the MCP server and for planning burn
    severity analysis.
    """
    from geo_processing.stac_client import search_sentinel2, tile_id_of, cloud_cover_of

    bbox = request.data.get('bbox')
    start_date = request.data.get('start_date')
    end_date = request.data.get('end_date')
    max_cloud_cover = request.data.get('max_cloud_cover', 30)
    try:
        limit = min(int(request.data.get('limit', 100)), 500)
    except (TypeError, ValueError):
        limit = 100

    if not all([bbox, start_date, end_date]):
        return Response(
            {'error': 'Missing required parameters: bbox, start_date, end_date'},
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

    try:
        items = search_sentinel2(
            bbox=bbox,
            start_date=start_date,
            end_date=end_date,
            max_cloud_cover=max_cloud_cover,
            limit=limit,
        )
    except Exception as e:
        logger.exception('Sentinel-2 search failed')
        return Response(
            {'error': f'Sentinel-2 search failed: {str(e)}'},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

    scenes = [
        {
            'id': s.id,
            'datetime': str(s.datetime),
            'cloud_cover': s.properties.get('eo:cloud_cover'),
            'tile': tile_id_of(s),
        }
        for s in items[:50]
    ]

    best_per_tile = {}
    for s in items:
        tile = tile_id_of(s)
        if not tile:
            continue
        cloud = cloud_cover_of(s)
        entry = best_per_tile.get(tile)
        if entry is None or cloud < entry['cloud_cover']:
            best_per_tile[tile] = {
                'tile': tile,
                'scene_id': s.id,
                'datetime': str(s.datetime),
                'cloud_cover': cloud,
            }

    return Response({
        'bbox': list(bbox),
        'start_date': start_date,
        'end_date': end_date,
        'max_cloud_cover': max_cloud_cover,
        'scenes_found': len(items),
        'tiles': sorted(best_per_tile.values(), key=lambda t: t['tile']),
        'scenes': scenes,
    })


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

    # Optimization: use datetime range instead of detected_at__date for index scan
    if date_from:
        try:
            dt_from = datetime.strptime(date_from, '%Y-%m-%d')
            fire_qs = fire_qs.filter(detected_at__gte=dt_from)
        except ValueError:
            pass
        burn_qs = burn_qs.filter(post_date__gte=date_from)
    if date_to:
        try:
            dt_to = datetime.strptime(date_to, '%Y-%m-%d') + timedelta(days=1)
            fire_qs = fire_qs.filter(detected_at__lt=dt_to)
        except ValueError:
            pass
        burn_qs = burn_qs.filter(post_date__lte=date_to)

    # Optimization: DB aggregation instead of loading all objects into Python
    # Single query for fire stats instead of multiple count() calls
    fire_agg = fire_qs.aggregate(
        total=models.Count('id'),
        high_confidence=models.Count('id', filter=Q(confidence='high')),
        avg_brightness=models.Avg('brightness'),
    )

    # Single query for burn stats instead of loading all objects
    burn_agg = burn_qs.aggregate(
        total_areas=models.Count('id'),
        total_area_ha=models.Sum('area_ha'),
    )

    stats = {
        'fires': {
            'total': fire_agg['total'] or 0,
            'high_confidence': fire_agg['high_confidence'] or 0,
            'avg_brightness': round(fire_agg['avg_brightness'] or 0, 1),
        },
        'burns': {
            'total_areas': burn_agg['total_areas'] or 0,
            'total_area_ha': round(burn_agg['total_area_ha'] or 0, 2),
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
