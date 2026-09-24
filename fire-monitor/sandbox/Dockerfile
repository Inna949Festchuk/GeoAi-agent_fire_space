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

# LasZip (драйвер сжатия LAS/LAZ) собирается из исходников: в Debian trixie
# нет готового пакета (laszip-bin только для bookworm). Ветка release/NN-MM
# в репозитории LASzip соответствует году/месяцу релиза, а не версии;
# стабильный тег 3.4.3 актуален (3.5.0 не собирается из-за регрессии:
# std::filesystem без <filesystem>). Синтаксис CMakeLists этого тега чистый,
# sed-патч больше не нужен. Компилятор затем удаляется на финальном этапе purge.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake zlib1g-dev git ca-certificates \
    && git clone --depth 1 --branch 3.4.3 \
        https://github.com/LASzip/LASzip.git /tmp/laszip \
    && cmake -S /tmp/laszip -B /tmp/laszip/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /tmp/laszip/build --parallel \
    && cmake --install /tmp/laszip/build \
    && ldconfig \
    && rm -rf /tmp/laszip /var/lib/apt/lists/*

# Лазерные python-библиотеки:
# - laszip 0.2.4 — экстра "laszip" для laspy (0.3.0 laspy 2.x не разрешает);
#   колесо manylinux_2_17 cp312 статически линкует свой LasZip, ставится с PyPI
#   без компиляции.
# - lazperf: на PyPI выложен только sdist lazperf-1.5 — готовых колёс под
#   cp312 нет, а sdist нерабочий: setup.py читает python/README.rst и
#   python/CHANGES.rst, которых нет в архиве (ошибка сборки
#   "FileNotFoundError: python/README.rst"). Системного python3-lazperf в
#   Debian trixie тоже нет. Поэтому ставим lazrs — поддерживаемый
#   LAZ-декодер laspy (экстра "lazrs", самодостаточное manylinux-колесо):
#   pip install "laspy[lazrs]==2.5.4" ниже.
# - PDAL: pip-дистрибутив НЕ содержит бинарного CLI — это pybind11-расширение,
#   которое собирается из исходников против установленной libpdal
#   (scikit-build-core + find_package(PDAL)). Ставится блоком geo-пакетов
#   вместе с GDAL==$(gdal-config --version).
RUN pip install --no-cache-dir "laszip==0.2.4" \
    && python -c "import laszip; print('laszip ok')"

# Компилируем PDAL 3.5.4 из исходников: даёт и бинарник pdal (CLI), и CMake
#-конфигурацию libpdal-python, необходимые для сборки pip-модуля pdal.
# Пул временных файлов (build-essential/cmake/git) — в следующем RUN-блоке.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates \
        libpdal-dev pdal \
    && git clone --depth 1 --branch 3.5.4 \
        https://github.com/PDAL/python.git /tmp/pdal-python \
    && cmake -S /tmp/pdal-python -B /tmp/pdal-python/build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build /tmp/pdal-python/build --parallel \
    && cmake --install /tmp/pdal-python/build \
    && ldconfig \
    && rm -rf /tmp/pdal-python /var/lib/apt/lists/* \
    && pdal --version

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
