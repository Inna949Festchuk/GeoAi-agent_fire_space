FROM python:3.12-slim

WORKDIR /app

COPY mcp_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp_server/ .

# Копируем geo_processing из backend (общий код для маршрутов и поиска пожарных частей)
COPY backend/geo_processing ./geo_processing

EXPOSE 8001

CMD ["python", "server.py"]
