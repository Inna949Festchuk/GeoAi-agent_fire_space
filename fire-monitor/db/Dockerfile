FROM postgis/postgis:16-3.4

# Add initialization scripts
COPY init/ /docker-entrypoint-initdb.d/

# Исправляем права доступа для SQL-скриптов
RUN chmod 644 /docker-entrypoint-initdb.d/*.sql

# Configure PostGIS
ENV POSTGIS_VERSION=3.4