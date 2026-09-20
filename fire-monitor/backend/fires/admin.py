from django.contrib import admin
from django.contrib.gis.admin import GISModelAdmin
from .models import FireHotspot, BurnArea, ProcessingJob


@admin.register(FireHotspot)
class FireHotspotAdmin(GISModelAdmin):
    list_display = ('source', 'confidence', 'brightness', 'frp', 'detected_at', 'is_verified')
    list_filter = ('source', 'confidence', 'detected_at', 'is_verified')
    search_fields = ('source',)
    readonly_fields = ('created_at',)


@admin.register(BurnArea)
class BurnAreaAdmin(GISModelAdmin):
    list_display = ('severity', 'area_ha', 'pre_date', 'post_date', 'satellite')
    list_filter = ('severity', 'satellite', 'pre_date')
    readonly_fields = ('processing_date',)


@admin.register(ProcessingJob)
class ProcessingJobAdmin(admin.ModelAdmin):
    list_display = ('job_type', 'status', 'created_at', 'started_at', 'completed_at')
    list_filter = ('job_type', 'status')
    readonly_fields = ('created_at',)
