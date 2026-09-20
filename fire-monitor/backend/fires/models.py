from django.contrib.gis.db import models


class FireHotspot(models.Model):
    """Active fire hotspot detected by satellite sensors."""

    SOURCE_CHOICES = [
        ('VIIRS_SNPP', 'VIIRS Suomi NPP'),
        ('VIIRS_NOAA20', 'VIIRS NOAA-20'),
        ('MODIS_Aqua', 'MODIS Aqua'),
        ('MODIS_Terra', 'MODIS Terra'),
    ]

    CONFIDENCE_CHOICES = [
        ('low', 'Low'),
        ('nominal', 'Nominal'),
        ('high', 'High'),
    ]

    location = models.PointField(srid=4326, db_index=True)
    brightness = models.FloatField(help_text='Brightness temperature in Kelvin')
    brightness_31 = models.FloatField(
        null=True, blank=True,
        help_text='3.1µm brightness temperature (MODIS only)'
    )
    confidence = models.CharField(max_length=10, choices=CONFIDENCE_CHOICES, default='nominal')
    frp = models.FloatField(
        null=True, blank=True,
        help_text='Fire Radiative Power in MW'
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, db_index=True)
    satellite = models.CharField(max_length=20, blank=True)
    instrument = models.CharField(max_length=20, blank=True)
    detected_at = models.DateTimeField(db_index=True)
    daynight = models.CharField(max_length=1, default='D', help_text='D=Day, N=Night')
    is_verified = models.BooleanField(default=False)
    filter_flag = models.CharField(
        max_length=50, blank=True,
        help_text='Reason if filtered out (e.g., industrial, reflection)'
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-detected_at']
        indexes = [
            models.Index(fields=['source', 'detected_at']),
            models.Index(fields=['confidence']),
        ]

    def __str__(self):
        return f"{self.source} fire at {self.location} ({self.confidence})"


class BurnArea(models.Model):
    """Burned area mapped from satellite imagery (Sentinel-2 dNBR)."""

    SEVERITY_CHOICES = [
        ('unburned', 'Unburned'),
        ('low', 'Low severity'),
        ('moderate', 'Moderate severity'),
        ('high', 'High severity'),
    ]

    geometry = models.MultiPolygonField(srid=4326, db_index=True)
    area_ha = models.FloatField(help_text='Burned area in hectares')
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, db_index=True)
    dnbr_mean = models.FloatField(null=True, blank=True, help_text='Mean dNBR value')
    pre_date = models.DateField(help_text='Pre-fire image date')
    post_date = models.DateField(help_text='Post-fire image date')
    satellite = models.CharField(max_length=30, default='Sentinel-2')
    tile_id = models.CharField(max_length=100, blank=True, help_text='Sentinel-2 tile ID')
    processing_date = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-pre_date']
        indexes = [
            models.Index(fields=['severity', 'pre_date']),
        ]

    def __str__(self):
        return f"Burn area {self.area_ha:.1f} ha ({self.severity})"


class ProcessingJob(models.Model):
    """Tracks processing jobs for fire detection and burn mapping."""

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    JOB_TYPE_CHOICES = [
        ('fire_detection', 'Fire Detection'),
        ('burn_mapping', 'Burn Severity Mapping'),
    ]

    bbox = models.PolygonField(srid=4326, help_text='Processing area bounding box')
    job_type = models.CharField(max_length=20, choices=JOB_TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    params = models.JSONField(default=dict, help_text='Job parameters')
    result_summary = models.JSONField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.job_type} [{self.status}] at {self.created_at}"


class IndustrialZone(models.Model):
    """Industrial zones and facilities for fire hotspot filtering."""

    ZONE_TYPE_CHOICES = [
        ('industrial', 'Industrial'),
        ('oil_and_gas', 'Oil & Gas'),
        ('power_plant', 'Power Plant'),
        ('quarry', 'Quarry'),
        ('mine', 'Mine'),
        ('factory', 'Factory'),
        ('warehouse', 'Warehouse'),
    ]

    geometry = models.MultiPolygonField(srid=4326, db_index=True)
    zone_type = models.CharField(max_length=50, choices=ZONE_TYPE_CHOICES, db_index=True)
    name = models.CharField(max_length=200, blank=True)
    source = models.CharField(max_length=50, default='overture_maps')
    source_id = models.CharField(max_length=100, blank=True, db_index=True, help_text='ID from source dataset')
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_updated']
        indexes = [
            models.Index(fields=['zone_type', 'source']),
        ]

    def __str__(self):
        return f"{self.zone_type}: {self.name or 'Unnamed'} ({self.source})"


class BurnReport(models.Model):
    """Analytical report summarizing burn severity analysis."""

    report_id = models.CharField(max_length=50, unique=True, db_index=True)
    generated_at = models.DateTimeField(auto_now_add=True)
    query_params = models.JSONField(help_text='Query parameters (bbox, dates)')
    summary = models.JSONField(help_text='Summary statistics')
    severity_distribution = models.JSONField(help_text='Breakdown by severity class')
    by_landcover = models.JSONField(null=True, blank=True, help_text='Breakdown by land cover type')
    data_quality = models.JSONField(null=True, blank=True, help_text='Data quality metrics')
    burn_areas = models.ManyToManyField(BurnArea, related_name='reports', blank=True)

    class Meta:
        ordering = ['-generated_at']

    def __str__(self):
        return f"Report {self.report_id} ({self.generated_at.date()})"
