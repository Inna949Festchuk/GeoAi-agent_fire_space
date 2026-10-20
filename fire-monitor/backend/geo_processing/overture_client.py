"""
Overture Maps client for querying industrial zones and land cover data.

Overture Maps provides open geospatial data including:
- Industrial zones and facilities
- Points of interest (POI)
- Land cover classification
- Building footprints

Data is stored as GeoParquet on a public AWS S3 bucket and queried via
DuckDB (httpfs).

The release version is read from the OVERTURE_RELEASE env var (default:
the latest release verified present in the bucket). Releases 2025+ use a
unified places theme (single type=place) with `basic_category` /
`taxonomy` fields instead of the old 2024-alpha `categories.main`.
Point POIs are materialized as small polygons when cached (the
IndustrialZone model stores MultiPolygon).
"""

import logging
import os
from typing import Optional, List, Dict, Any
import duckdb

logger = logging.getLogger(__name__)

# Overture Maps S3 bucket (public read)
OVERTURE_S3_BUCKET = os.environ.get('OVERTURE_S3_BUCKET', 'overturemaps-us-west-2')

# Release version — override with the OVERTURE_RELEASE env var.
# Old releases are purged from the bucket; the default is the newest one
# verified available (2026-10-20).
OVERTURE_RELEASE = os.environ.get('OVERTURE_RELEASE', '2026-09-23.1')

# S3 paths per theme (type=* glob stays compatible across release layouts)
OVERTURE_PATHS = {
    'places': f'release/{OVERTURE_RELEASE}/theme=places/type=*/*',
    'buildings': f'release/{OVERTURE_RELEASE}/theme=buildings/type=*/*',
    'transportation': f'release/{OVERTURE_RELEASE}/theme=transportation/type=*/*',
    'base': f'release/{OVERTURE_RELEASE}/theme=base/type=*/*',
}

# Industrial keywords for the 2026 places taxonomy — matched with ILIKE
# against basic_category and taxonomy.primary.
INDUSTRIAL_KEYWORDS = [
    'industrial', 'factory', 'manufactur', 'mine', 'mining', 'quarry',
    'power', 'oil', 'gas', 'refin', 'chemical', 'warehouse', 'storage',
]

# Retail/consumer matches of the keywords above — excluded from zones.
# (gas stations, gastro-pubs, oil-change shops, self-storage lockers)
EXCLUDED_BASIC_CATEGORIES = ['gas_station']
EXCLUDED_TAXONOMY_PRIMARY = [
    'gastropub', 'oil_change_station', 'self_storage_facility',
]

# Point POIs are materialized as a polygon of this radius (degrees,
# roughly a hundred meters at mid latitudes).
POINT_BUFFER_DEG = 0.001


def zone_type_from_category(category: str, taxonomy_primary: str) -> str:
    """Map a 2026 Overture category to IndustrialZone.ZONE_TYPE_CHOICES."""
    text = f'{category or ""} {taxonomy_primary or ""}'.lower()
    if 'power' in text or 'electric' in text:
        return 'power_plant'
    if 'oil' in text or 'gas' in text or 'refin' in text:
        return 'oil_and_gas'
    if 'quarry' in text:
        return 'quarry'
    if 'mining' in text or 'mine' in text:
        return 'mine'
    if 'factory' in text or 'manufactur' in text:
        return 'factory'
    if 'warehouse' in text or 'storage' in text or 'logistic' in text:
        return 'warehouse'
    return 'industrial'


class OvertureClient:
    """Client for querying Overture Maps data from S3 (DuckDB + httpfs)."""

    def __init__(self):
        """Initialize DuckDB connection with anonymous S3 access."""
        self.conn = duckdb.connect()
        self.conn.execute("SET s3_region='us-west-2'")
        # httpfs — S3 access; spatial — ST_* geometry functions. INSTALL
        # from the public extension repo when missing (fresh container).
        for ext in ('httpfs', 'spatial'):
            try:
                self.conn.execute(f'LOAD {ext}')
            except Exception:
                self.conn.execute(f'INSTALL {ext}')
                self.conn.execute(f'LOAD {ext}')

        logger.info(f'OvertureClient initialized (release {OVERTURE_RELEASE})')

    def _category_filter(self, keywords: List[str]) -> str:
        return ' OR '.join(
            f"(basic_category ILIKE '%{kw}%' OR taxonomy.primary ILIKE '%{kw}%')"
            for kw in keywords
        )

    def _exclusion_filter(self) -> str:
        """NOT-clause dropping retail/consumer keyword matches (see module lists)."""
        parts = []
        if EXCLUDED_BASIC_CATEGORIES:
            cats = ', '.join(f"'{c}'" for c in EXCLUDED_BASIC_CATEGORIES)
            parts.append(f'AND basic_category NOT IN ({cats})')
        if EXCLUDED_TAXONOMY_PRIMARY:
            prim = ', '.join(f"'{p}'" for p in EXCLUDED_TAXONOMY_PRIMARY)
            parts.append(f'AND taxonomy.primary NOT IN ({prim})')
        return '\n                '.join(parts)

    def query_industrial_zones(
        self,
        bbox: tuple,
        keywords: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Query industrial zones and facilities from Overture Places.

        Args:
            bbox: Bounding box (minx, miny, maxx, maxy) in WGS84
            keywords: category keywords to filter (default: industrial set)

        Returns:
            List of zone records with WKT geometry and properties.
        """
        if keywords is None:
            keywords = INDUSTRIAL_KEYWORDS

        minx, miny, maxx, maxy = bbox
        places_path = OVERTURE_PATHS['places']

        query = f"""
            SELECT
                id,
                COALESCE(names.primary, '') AS name,
                basic_category,
                taxonomy.primary AS taxonomy_primary,
                ST_AsText(geometry) AS geometry_wkt
            FROM read_parquet('s3://{OVERTURE_S3_BUCKET}/{places_path}')
            WHERE
                bbox.xmin <= {maxx} AND bbox.xmax >= {minx}
                AND bbox.ymin <= {maxy} AND bbox.ymax >= {miny}
                AND geometry IS NOT NULL
                AND ({self._category_filter(keywords)})
                {self._exclusion_filter()}
        """

        try:
            result = self.conn.execute(query).fetchall()
            columns = ['id', 'name', 'basic_category', 'taxonomy_primary',
                       'geometry_wkt']
            zones = [dict(zip(columns, row)) for row in result]

            logger.info(f'Found {len(zones)} industrial zones in bbox')
            return zones

        except Exception as e:
            logger.error(f'Failed to query industrial zones: {e}')
            return []

    def query_poi(
        self,
        bbox: tuple,
        keywords: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Query points of interest from Overture Places.

        Args:
            bbox: Bounding box (minx, miny, maxx, maxy) in WGS84
            keywords: category keywords to filter

        Returns:
            List of POI records with WKT geometry and properties.
        """
        if keywords is None:
            keywords = ['oil', 'gas', 'power', 'mine', 'quarry']

        minx, miny, maxx, maxy = bbox
        places_path = OVERTURE_PATHS['places']

        query = f"""
            SELECT
                id,
                COALESCE(names.primary, '') AS name,
                basic_category,
                ST_X(geometry) AS longitude,
                ST_Y(geometry) AS latitude,
                ST_AsText(geometry) AS geometry_wkt
            FROM read_parquet('s3://{OVERTURE_S3_BUCKET}/{places_path}')
            WHERE
                bbox.xmin <= {maxx} AND bbox.xmax >= {minx}
                AND bbox.ymin <= {maxy} AND bbox.ymax >= {miny}
                AND geometry IS NOT NULL
                AND ({self._category_filter(keywords)})
        """

        try:
            result = self.conn.execute(query).fetchall()
            columns = ['id', 'name', 'basic_category', 'longitude',
                       'latitude', 'geometry_wkt']
            pois = [dict(zip(columns, row)) for row in result]

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

        Note: land cover query is not implemented for the current release.
        """
        logger.warning('Land cover query not yet implemented for Overture Maps')
        return []

    def close(self):
        """Close DuckDB connection."""
        if self.conn:
            self.conn.close()
            logger.info('OvertureClient connection closed')


def cache_industrial_zones_to_db(
    bbox: tuple,
    client: Optional['OvertureClient'] = None,
    keywords: Optional[List[str]] = None,
) -> int:
    """
    Query industrial zones from Overture and cache them in PostGIS database.

    Point POIs are buffered to small polygons so the IndustrialZone model
    (MultiPolygon) and the 500 m zone buffers used by validation work
    uniformly for both area and point features.

    Args:
        bbox: Bounding box to query
        client: Optional OvertureClient instance (will create one if not provided)
        keywords: category keywords (default: industrial set)

    Returns:
        Number of zones created
    """
    from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
    from fires.models import IndustrialZone

    if client is None:
        client = OvertureClient()

    try:
        zones = client.query_industrial_zones(bbox, keywords)

        cached_count = 0
        for zone in zones:
            try:
                # Parse WKT geometry
                geom = GEOSGeometry(zone['geometry_wkt'], srid=4326)

                # Point POI -> small polygon (~100 m radius)
                if geom.geom_type == 'Point':
                    geom = geom.buffer(POINT_BUFFER_DEG)

                # Convert to MultiPolygon if needed
                if geom.geom_type == 'Polygon':
                    geom = MultiPolygon([geom])
                elif geom.geom_type != 'MultiPolygon':
                    continue

                zone_type = zone_type_from_category(
                    zone.get('basic_category'), zone.get('taxonomy_primary'))

                # Create or update zone
                _, created = IndustrialZone.objects.update_or_create(
                    source_id=zone['id'],
                    defaults={
                        'geometry': geom,
                        'zone_type': zone_type,
                        'name': (zone.get('name')
                                 or zone.get('basic_category') or '')[:200],
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