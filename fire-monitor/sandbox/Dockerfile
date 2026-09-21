FROM python:3.12-slim

# Системные зависимости для гео-библиотек
RUN apt-get update && apt-get install -y \
    libgeos-dev \
    libproj-dev \
    gdal-bin \
    libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

# Фиксированные версии для воспроизводимости
RUN pip install --no-cache-dir \
    fastapi==0.115.0 \
    uvicorn==0.32.0 \
    pydantic==2.9.2 \
    numpy==1.26.4 \
    pandas==2.2.3 \
    geopandas==1.0.1 \
    shapely==2.0.6 \
    slowapi==0.1.9

WORKDIR /app
COPY executor.py .

# Создаем непривилегированного пользователя
RUN useradd -m -r sandbox_user && chown -R sandbox_user:sandbox_user /app
USER sandbox_user

EXPOSE 8002
CMD ["uvicorn", "executor:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]
