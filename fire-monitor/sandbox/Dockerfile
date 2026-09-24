FROM python:3.12-slim

# Системные зависимости для гео-библиотек:
# - GDAL/PROJ/GEOS: векторные и растровые геоданные, проекции
# - NetCDF/HDF5: спутниковые и климатические форматы (CDS/ERA5, MODIS)
# - rasterio/GDAL wheels требуют libgdal
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgeos-dev \
    libproj-dev \
    proj-data \
    proj-bin \
    gdal-bin \
    libgdal-dev \
    libnetcdf-dev \
    libhdf5-dev \
    libspatialindex-dev \
    libgeotiff-dev \
    libjson-c-dev \
    libxml2-dev \
    libssl-dev \
    libcurl4-openssl-dev \
    libboost-dev \
    libunwind-dev \
    libzstd-dev \
    ca-certificates \
    git \
    && rm -rf /var/lib/apt/lists/*

# LasZip + LAZperf (драйверы сжатия LAS/LAZ) собираются из исходников:
# в Debian нет готовых пакетов. Единая системная liblaszip/liblazperf нужна,
# чтобы PDAL использовал один и тот же код сжатия. Компилятор затем удаляется
# на финальном этапе purge.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake zlib1g-dev git ca-certificates \
    && . /etc/os-release \
    && git clone --depth 1 --branch "release/${VERSION_ID}-02" \
        https://github.com/LASzip/LASzip.git /tmp/laszip \
    # в CMakeLists этого тега есть синтаксическая ошибка — чиним патчем
    && sed -i 's/^IF(CMAKE_COMPILER_IS_GNUCXX)$/' /tmp/laszip/CMakeLists.txt \
    && cmake -S /tmp/laszip -B /tmp/laszip/build -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
    && cmake --build /tmp/laszip/build --parallel \
    && cmake --install /tmp/laszip/build \
    && ldconfig \
    && git clone --depth 1 https://github.com/hobu/laz-perf.git /tmp/lazperf \
    && cmake -S /tmp/lazperf -B /tmp/lazperf/build -DCMAKE_BUILD_TYPE=Release \
        -DLAZPERF_TESTS_DISABLED=ON \
    && cmake --build /tmp/lazperf/build --parallel \
    && cmake --install /tmp/lazperf/build \
    && ldconfig \
    && rm -rf /tmp/laszip /tmp/lazperf /var/lib/apt/lists/*

# Python-биндинг laszip (экстра "laszip" для laspy) ставим колесом manylinux_2_17:
# версия 0.2.4 — последняя, разрешённая laspy 2.x (лазер <0.3.0). Колесо
# самодостаточное (статически линкует свой LasZip), отдельной сборки не требует.
RUN pip install --no-cache-dir "laszip==0.2.4"

# Системный PDAL (CLI + библиотеки) собирается из исходников: так получаем
# laszip/lazperf-сжатие (библиотеки выше) и версию, точно совпадающую с
# pip-обёрткой PDAL (ctypes). Путь к LasZip подсказываем явно (PDAL ищет
# pkg-config-файл laszip.pc, которого у LasZip нет). Если LasZip не найден —
# сборка падает здесь (fail-fast), а не тихо теряет поддержку LAZ.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates \
    && test -f /usr/local/include/laszip/laszip.hpp \
    && test -f /usr/local/lib/liblaszip.so \
    && git clone --depth 1 --branch pdal-3.4.1 \
        https://github.com/PDAL/PDAL.git /tmp/pdal \
    && cmake -S /tmp/pdal -B /tmp/pdal/build -DCMAKE_BUILD_TYPE=Release \
        -DPDAL_BUILD_STATIC=OFF \
        -DWITH_LASZIP=ON -DWITH_LAZPERF=ON \
        -DLASZIP_INCLUDE_DIR=/usr/local/include \
        -DLASZIP_LIBRARY=/usr/local/lib/liblaszip.so \
        -DWITH_E57=OFF -DWITH_NITRO=OFF \
        -DBUILD_PLUGIN_TILEDB=OFF -DBUILD_DRIVER_pgPOINT=OFF \
    && cmake --build /tmp/pdal/build --parallel \
    # проверяем на этапе сборки, что драйвер readers.las/writers.las собраны
    && /tmp/pdal/build/bin/pdal --drivers | grep -E "(readers|writers)\.las" \
    && cmake --install /tmp/pdal/build \
    && ldconfig \
    && rm -rf /tmp/pdal /var/lib/apt/lists/*

# Устанавливаем Python-пакеты поверх СИСТЕМНЫХ GDAL/PROJ из Debian:
# - C extensions собираем из исходников (build isolation отключен, чтобы
#   компилятор видел заголовки libgdal-dev/libgeos-dev)
# - растровые/векторные форматы и драйверы обеспечивает системный gdal-bin
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    cmake \
    && pip install --no-cache-dir \
        fastapi==0.115.0 \
        uvicorn==0.32.0 \
        pydantic==2.9.2 \
        numpy==1.26.4 \
        pandas==2.2.3 \
        shapely==2.0.6 \
        slowapi==0.1.9 \
        pyproj==3.8.1 \
        geopy==2.4.1 \
        xarray==2025.1.2 \
        scipy==1.13.1 \
        scikit-image==0.24.0 \
        matplotlib==3.9.2 \
    # geo-пакеты, требующие сборки против системных библиотек
    # (gdal-config задаёт версию GDAL == версии C-библиотеки в образе)
    && pip install --no-cache-dir --no-build-isolation \
        GDAL==$(gdal-config --version) \
        rasterio==1.3.11 \
        pyogrio==0.8.0 \
        netCDF4==1.7.2 \
        h5py==3.12.1 \
        h5netcdf==1.6.1 \
        PDAL==$(pdal --version | sed 's/[^0-9.]//g') \
    # чистые Python пакеты (зависимые от gdal/rasterio API)
    && pip install --no-cache-dir \
        geopandas==1.0.1 \
        rioxarray==0.15.1 \
        rio-cogeo==5.3.1 \
        geoalchemy2==0.15.2 \
        mercantile==1.2.1 \
        laspy==2.5.4 \
        cligj>=0.5 \
        click-plugins \
        pyparsing \
        snuggs>=1.4.1 \
        affine \
    # удаляем тулчейн сборки — уменьшает поверхность атаки и размер образа
    && apt-get purge -y --auto-remove build-essential python3-dev cmake \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

# Матplotlib: рендерим графики в PNG (Agg), шрифты кэшируем на этапе сборки,
# чтобы рантайм в read-only контейнере не пытался писать ~/.config/matplotlib.
ENV MPLBACKEND=Agg \
    MPLCONFIGDIR=/app/.matplotlib
RUN mkdir -p /app/.matplotlib \
    && python -c "import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt; plt.plot([0,1],[0,1]); plt.savefig('/tmp/_mpl_check.png')" \
    && rm -f /tmp/_mpl_check.png

# PROJ: не скачиваем сетку геодезических трансформаций при первом запуске
# (образ read-only и без сети) — используем встроенные данные proj-data
ENV PROJ_NETWORK=OFF

WORKDIR /app
COPY executor.py .

# Создаем непривилегированного пользователя
RUN useradd -m -r sandbox_user && chown -R sandbox_user:sandbox_user /app
USER sandbox_user

EXPOSE 8002
CMD ["uvicorn", "executor:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]
