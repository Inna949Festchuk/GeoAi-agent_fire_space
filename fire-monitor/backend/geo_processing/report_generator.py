"""
Burn report generator for creating analytical summaries.

Generates structured reports with:
- Severity distribution
- Land cover breakdown
- Data quality metrics
- Summary statistics
"""

import logging
import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional

from django.contrib.gis.geos import Polygon
from django.db.models import Sum, Count, Avg

logger = logging.getLogger(__name__)


class BurnReportGenerator:
    """Generator for burn severity analytical reports."""

    def __init__(self):
        """Initialize report generator."""
        self.report_id = None

    def generate_report(
        self,
        bbox: tuple,
        date_from: str,
        date_to: str,
        include_landcover: bool = True,
    ) -> Dict[str, Any]:
        """
        Generate a comprehensive burn report.

        Args:
            bbox: Bounding box (minx, miny, maxx, maxy)
            date_from: Start date (YYYY-MM-DD)
            date_to: End date (YYYY-MM-DD)
            include_landcover: Whether to include land cover analysis

        Returns:
            Dict with report data
        """
        from fires.models import BurnArea, BurnReport

        # Generate unique report ID
        self.report_id = f"burn_{uuid.uuid4().hex[:12]}"

        logger.info(f'Generating report {self.report_id} for bbox={bbox}, dates={date_from} to {date_to}')

        # Query burn areas in bbox and date range
        bbox_geom = Polygon.from_bbox(bbox)
        burn_areas = BurnArea.objects.filter(
            geometry__intersects=bbox_geom,
            post_date__gte=date_from,
            post_date__lte=date_to,
        )

        if not burn_areas.exists():
            logger.warning('No burn areas found for the specified parameters')
            return {
                'report_id': self.report_id,
                'error': 'No burn areas found for the specified parameters',
            }

        # Calculate statistics
        summary = self.calculate_summary(burn_areas)
        severity_distribution = self.calculate_severity_distribution(burn_areas)

        # Land cover analysis (optional)
        by_landcover = None
        if include_landcover:
            by_landcover = self.calculate_landcover_breakdown(burn_areas, bbox)

        # Data quality metrics
        data_quality = self.calculate_data_quality(burn_areas)

        # Create report record
        report = BurnReport.objects.create(
            report_id=self.report_id,
            query_params={
                'bbox': list(bbox),
                'date_from': date_from,
                'date_to': date_to,
            },
            summary=summary,
            severity_distribution=severity_distribution,
            by_landcover=by_landcover,
            data_quality=data_quality,
        )

        # Link burn areas to report
        report.burn_areas.set(burn_areas)

        logger.info(f'Report {self.report_id} generated successfully')

        return {
            'report_id': self.report_id,
            'generated_at': report.generated_at.isoformat(),
            'query_params': report.query_params,
            'summary': summary,
            'severity_distribution': severity_distribution,
            'by_landcover': by_landcover,
            'data_quality': data_quality,
        }

    def calculate_summary(self, burn_areas) -> Dict[str, Any]:
        """
        Calculate summary statistics for burn areas.

        Args:
            burn_areas: QuerySet of BurnArea objects

        Returns:
            Dict with summary statistics
        """
        total_area_ha = sum(b.area_ha for b in burn_areas)
        total_polygons = burn_areas.count()

        # Calculate by severity
        by_severity = {}
        for severity in ['low', 'moderate', 'high']:
            severity_areas = burn_areas.filter(severity=severity)
            if severity_areas.exists():
                by_severity[severity] = {
                    'count': severity_areas.count(),
                    'area_ha': round(sum(b.area_ha for b in severity_areas), 2),
                }

        # Date range
        dates = burn_areas.values_list('post_date', flat=True).distinct()
        date_range = {
            'earliest': min(dates).isoformat() if dates else None,
            'latest': max(dates).isoformat() if dates else None,
        }

        return {
            'total_burn_area_ha': round(total_area_ha, 2),
            'burn_polygons_count': total_polygons,
            'by_severity': by_severity,
            'date_range': date_range,
        }

    def calculate_severity_distribution(self, burn_areas) -> Dict[str, Any]:
        """
        Calculate detailed severity distribution.

        Args:
            burn_areas: QuerySet of BurnArea objects

        Returns:
            Dict with severity breakdown
        """
        total_area = sum(b.area_ha for b in burn_areas)

        distribution = {}
        for severity in ['unburned', 'low', 'moderate', 'high']:
            severity_areas = burn_areas.filter(severity=severity)
            if severity_areas.exists():
                area_ha = sum(b.area_ha for b in severity_areas)
                percentage = (area_ha / total_area * 100) if total_area > 0 else 0

                distribution[severity] = {
                    'area_ha': round(area_ha, 2),
                    'percentage': round(percentage, 1),
                    'count': severity_areas.count(),
                    'mean_dnbr': round(
                        severity_areas.aggregate(Avg('dnbr_mean'))['dnbr_mean__avg'] or 0,
                        1
                    ),
                }

        return distribution

    def calculate_landcover_breakdown(
        self,
        burn_areas,
        bbox: tuple,
    ) -> Optional[Dict[str, Any]]:
        """
        Calculate land cover breakdown for burn areas.

        Note: This is a placeholder. Full implementation requires
        integration with Overture Maps land cover data.

        Args:
            burn_areas: QuerySet of BurnArea objects
            bbox: Bounding box for land cover query

        Returns:
            Dict with land cover breakdown or None if not available
        """
        # Placeholder: In production, integrate with OvertureClient
        # to get actual land cover data

        # For now, return a simple breakdown based on severity
        # This should be replaced with actual land cover analysis

        logger.info('Land cover breakdown not yet fully implemented')

        # Return None to indicate land cover data is not available
        return None

    def calculate_data_quality(self, burn_areas) -> Dict[str, Any]:
        """
        Calculate data quality metrics.

        Args:
            burn_areas: QuerySet of BurnArea objects

        Returns:
            Dict with data quality metrics
        """
        # Count unique scenes used
        unique_scenes = burn_areas.values_list('tile_id', flat=True).distinct().count()

        # Date coverage
        dates = burn_areas.values_list('post_date', flat=True).distinct()
        date_count = len(dates)

        # Average cloud cover (if available in metadata)
        # Note: This would require storing cloud cover in BurnArea model

        return {
            'scenes_used': unique_scenes,
            'date_count': date_count,
            'satellite': 'Sentinel-2',
            'resolution_m': 10,
            'notes': 'Cloud masking applied using SCL band',
        }


def get_report(report_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve a generated report by ID.

    Args:
        report_id: Report identifier

    Returns:
        Dict with report data or None if not found
    """
    from fires.models import BurnReport

    try:
        report = BurnReport.objects.get(report_id=report_id)

        return {
            'report_id': report.report_id,
            'generated_at': report.generated_at.isoformat(),
            'query_params': report.query_params,
            'summary': report.summary,
            'severity_distribution': report.severity_distribution,
            'by_landcover': report.by_landcover,
            'data_quality': report.data_quality,
        }

    except BurnReport.DoesNotExist:
        return None


def list_reports(limit: int = 10) -> List[Dict[str, Any]]:
    """
    List recent reports.

    Args:
        limit: Maximum number of reports to return

    Returns:
        List of report summaries
    """
    from fires.models import BurnReport

    reports = BurnReport.objects.all()[:limit]

    return [
        {
            'report_id': r.report_id,
            'generated_at': r.generated_at.isoformat(),
            'query_params': r.query_params,
            'summary': r.summary,
        }
        for r in reports
    ]
