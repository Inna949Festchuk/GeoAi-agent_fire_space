"""
Overture Maps client for querying industrial zones and land cover data.

Overture Maps provides open geospatial data including:
- Industrial zones and facilities
- Points of interest (POI)
- Land cover classification
- Building footprints

Data is stored as GeoParquet on AWS S3 and can be queried efficiently using DuckDB.
"""

import logging
from typing import Optional, List, Dict, Any
import duckdb

logger = logging.getLogger(__name__)

# Overture Maps S3 bucket
OVERTURE_S3_BUCKET = 'overturemaps-us-west-2'

# Overture Maps release version
OVERTURE_RELEASE = '2024-02-15-alpha.0'

# S3 paths for different themes
OVERTURE_PATHS = {
    'places': f'release/{OVERTURE_RELEASE}/theme=places/type=*/*',
    'buildings': f'release/{OVERTURE_RELEASE}/theme=buildings/type=*/*',
    'transportation': f'release/{OVERTURE_RELEASE}/theme=transportation/type=*/*',
    'base': f'release/{OVERTURE_RELEASE}/theme=base/type=*/*',
}


class OvertureClient:
    """Client for querying Overture Maps data from S3."""

    def __init__(self):
        """Initialize DuckDB connection with S3 access."""
        self.conn = duckdb.connect()

        # Configure S3 access (anonymous, public bucket)
        self.conn.execute("""
            INSTALL httpfs;
            LOAD httpfs;
            SET s3_region='us-west-2';
        """)

        logger.info('OvertureClient initialized')

    def query_industrial_zones(
        self,
        bbox: tuple,
        categories: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Query industrial zones and facilities from Overture Places.

        Args:
            bbox: Bounding box (minx, miny, maxx, maxy) in WGS84
            categories: List of category names to filter (default: industrial categories)

        Returns:
            List of industrial zone records with geometry and properties
        """
        if categories is None:
            # Default industrial categories
            categories = [
                'industrial',
                'oil_and_gas',
                'power_plant',
                'quarry',
                'mine',
                'factory',
                'warehouse',
            ]

        minx, miny, maxx, maxy = bbox

        # Build category filter
        category_filter = ' OR '.join([
            f"categories.main ILIKE '%{cat}%'" for cat in categories
        ])

        query = f"""
            SELECT
                id,
                names.primary as name,
                categories.main as category,
                categories.alternate as alternate_categories,
                ST_AsText(geometry) as geometry_wkt,
                bbox.xmin as bbox_xmin,
                bbox.ymin as bbox_ymin,
                bbox.xmax as bbox_xmax,
                bbox.ymax as bbox_ymax
            FROM read_parquet('s3://{OVERTURE_S3_BUCKET}/{OVERTURE_PATHS['places']}')
            WHERE
                bbox.xmin <= {maxx} AND bbox.xmax >= {minx}
                AND bbox.ymin <= {maxy} AND bbox.ymax >= {miny}
                AND ({category_filter})
        """

        try:
            result = self.conn.execute(query).fetchall()
            columns = ['id', 'name', 'category', 'alternate_categories',
                      'geometry_wkt', 'bbox_xmin', 'bbox_ymin', 'bbox_xmax', 'bbox_ymax']

            zones = []
            for row in result:
                zone = dict(zip(columns, row))
                zones.append(zone)

            logger.info(f'Found {len(zones)} industrial zones in bbox')
            return zones

        except Exception as e:
            logger.error(f'Failed to query industrial zones: {e}')
            return []

    def query_poi(
        self,
        bbox: tuple,
        categories: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Query points of interest from Overture Places.

        Args:
            bbox: Bounding box (minx, miny, maxx, maxy) in WGS84
            categories: List of category names to filter

        Returns:
            List of POI records with geometry and properties
        """
        if categories is None:
            categories = ['oil_and_gas', 'power_plant', 'quarry', 'mine']

        minx, miny, maxx, maxy = bbox

        category_filter = ' OR '.join([
            f"categories.main ILIKE '%{cat}%'" for cat in categories
        ])

        query = f"""
            SELECT
                id,
                names.primary as name,
                categories.main as category,
                ST_X(geometry) as longitude,
                ST_Y(geometry) as latitude,
                ST_AsText(geometry) as geometry_wkt
            FROM read_parquet('s3://{OVERTURE_S3_BUCKET}/{OVERTURE_PATHS['places']}')
            WHERE
                bbox.xmin <= {maxx} AND bbox.xmax >= {minx}
                AND bbox.ymin <= {maxy} AND bbox.ymax >= {miny}
                AND ({category_filter})
        """

        try:
            result = self.conn.execute(query).fetchall()
            columns = ['id', 'name', 'category', 'longitude', 'latitude', 'geometry_wkt']

            pois = []
            for row in result:
                poi = dict(zip(columns, row))
                pois.append(poi)

            logger.info(f'Found {len(pois)} POIs in bbox')
            return pois

        except Exception as e:
            logger.error(f'Failed to query POIs: {e}')
            return []

    def query_landcover(
        self,
        bbox: tuple,
    ) -> List[Dict[str, Any]]:
        """
        Query land cover data from Overture Base.

        Note: Land cover data may not be available in all releases.
        This is a placeholder for future implementation.

        Args:
            bbox: Bounding box (minx, miny, maxx, maxy) in WGS84

        Returns:
            List of land cover records
        """
        logger.warning('Land cover query not yet implemented for Overture Maps')
        return []

    def close(self):
        """Close DuckDB connection."""
        if self.conn:
            self.conn.close()
            logger.info('OvertureClient connection closed')


def cache_industrial_zones_to_db(bbox: tuple, client: Optional[OvertureClient] = None):
    """
    Query industrial zones from Overture and cache them in PostGIS database.

    Args:
        bbox: Bounding box to query
        client: Optional OvertureClient instance (will create one if not provided)

    Returns:
        Number of zones cached
    """
    from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Polygon
    from fires.models import IndustrialZone

    if client is None:
        client = OvertureClient()

    try:
        zones = client.query_industrial_zones(bbox)

        cached_count = 0
        for zone in zones:
            try:
                # Parse WKT geometry
                geom = GEOSGeometry(zone['geometry_wkt'], srid=4326)

                # Convert to MultiPolygon if needed
                if geom.geom_type == 'Polygon':
                    geom = MultiPolygon([geom])
                elif geom.geom_type != 'MultiPolygon':
                    continue

                # Create or update zone
                industrial_zone, created = IndustrialZone.objects.update_or_create(
                    source_id=zone['id'],
                    defaults={
                        'geometry': geom,
                        'zone_type': zone.get('category', 'industrial'),
                        'name': zone.get('name', '')[:200],
                        'source': 'overture_maps',
                    }
                )

                if created:
                    cached_count += 1

            except Exception as e:
                logger.warning(f'Failed to cache zone {zone.get("id")}: {e}')
                continue

        logger.info(f'Cached {cached_count} industrial zones to database')
        return cached_count

    finally:
        if client is not None:
            client.close()
