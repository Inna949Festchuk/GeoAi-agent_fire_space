from rest_framework_gis.serializers import GeoFeatureModelSerializer
from rest_framework import serializers
from .models import FireHotspot, BurnArea, ProcessingJob, BurnReport


class FireHotspotSerializer(GeoFeatureModelSerializer):
    class Meta:
        model = FireHotspot
        geo_field = 'location'
        fields = [
            'id', 'brightness', 'brightness_31', 'confidence', 'frp',
            'source', 'satellite', 'instrument', 'detected_at',
            'daynight', 'is_verified', 'filter_flag', 'created_at',
        ]


class BurnAreaSerializer(GeoFeatureModelSerializer):
    class Meta:
        model = BurnArea
        geo_field = 'geometry'
        fields = [
            'id', 'area_ha', 'severity', 'dnbr_mean',
            'pre_date', 'post_date', 'satellite', 'tile_id',
            'processing_date',
        ]


class ProcessingJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcessingJob
        fields = [
            'id', 'job_type', 'status', 'params',
            'result_summary', 'error_message',
            'started_at', 'completed_at', 'created_at',
        ]


class ChatMessageSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=4000)
    bbox = serializers.ListField(
        child=serializers.FloatField(),
        required=False,
        min_length=4,
        max_length=4,
        help_text='Optional bounding box [minx, miny, maxx, maxy]'
    )


class FireStatsSerializer(serializers.Serializer):
    total_hotspots = serializers.IntegerField()
    by_source = serializers.DictField()
    by_confidence = serializers.DictField()
    total_burn_area_ha = serializers.FloatField()
    burn_by_severity = serializers.DictField()
    date_range = serializers.DictField()


class ReportGenerateSerializer(serializers.Serializer):
    """Serializer for generating burn reports."""
    bbox = serializers.ListField(
        child=serializers.FloatField(),
        min_length=4,
        max_length=4,
        help_text='Bounding box [minx, miny, maxx, maxy] in WGS84'
    )
    date_from = serializers.DateField(
        help_text='Start date (YYYY-MM-DD)'
    )
    date_to = serializers.DateField(
        help_text='End date (YYYY-MM-DD)'
    )
    include_landcover = serializers.BooleanField(
        default=True,
        required=False,
        help_text='Include land cover analysis'
    )


class BurnReportSerializer(serializers.ModelSerializer):
    """Serializer for burn reports."""
    class Meta:
        model = BurnReport
        fields = [
            'report_id',
            'generated_at',
            'query_params',
            'summary',
            'severity_distribution',
            'by_landcover',
            'data_quality',
        ]
        read_only_fields = fields
