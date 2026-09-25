import ast
import json
import sys
import asyncio
import logging
import resource  # 🛡️ Жесткие лимиты ОС
import atexit
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stdout, redirect_stderr
import io

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

# --- ЛОГИРОВАНИЕ ДЛЯ АУДИТА ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Ultra-Hardened Geo Sandbox")

# --- RATE LIMITING ---
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Try again later."}
    )

# --- НАСТРОЙКИ БЕЗОПАСНОСТИ ---
MAX_EXECUTION_TIME = 30  # секунд
MAX_RESULT_SIZE_MB = 5   # мегабайт
MAX_CONTEXT_SIZE_MB = 10 # мегабайт

# Запрещенные имена
FORBIDDEN_NAMES = {
    'os', 'sys', 'subprocess', 'eval', 'exec', 'compile',
    '__import__', 'open', 'input', 'getattr', 'setattr', 'delattr',
    'globals', 'locals', 'vars', 'dir', 'object',
    'breakpoint', 'exit', 'quit', 'help', 'memoryview',
    'shutil', 'pathlib', 'socket', 'http', 'urllib', 'requests'
}

# 🛡️ ИСПРАВЛЕНО: Добавлен 'mro' для блокировки MRO-обхода
FORBIDDEN_ATTRS = {
    '__builtins__', '__globals__', '__locals__', '__class__',
    '__base__', '__bases__', '__subclasses__', '__mro__', 'mro',
    '__dict__', '__code__', '__closure__', '__func__', '__self__',
    '__module__', '__import__', '__spec__', '__loader__', '__package__'
}

# 🛡️ ИСПРАВЛЕНО: Убран 'type' (позволяет обойти через type().__class__.__mro__)
# Расширенный набор builtins для геоаналитики
SAFE_BUILTINS = {
    # Математика
    'abs': abs, 'round': round, 'pow': pow, 'min': min, 'max': max, 'sum': sum,
    'divmod': divmod,
    # Преобразования типов
    'int': int, 'float': float, 'str': str, 'bool': bool, 'list': list, 
    'dict': dict, 'tuple': tuple, 'set': set, 'frozenset': frozenset,
    # Итерации и коллекции
    'range': range, 'enumerate': enumerate, 'zip': zip, 'map': map, 
    'filter': filter, 'sorted': sorted, 'reversed': reversed, 
    'iter': iter, 'next': next, 'len': len,
    # Проверки типов
    'isinstance': isinstance, 'issubclass': issubclass, 
    'hasattr': hasattr, 'callable': callable,
    # Форматирование и представление
    'repr': repr, 'format': format, 'ord': ord, 'chr': chr,
    'hex': hex, 'oct': oct, 'bin': bin,
    # Логика
    'all': all, 'any': any,
    # Ввод/вывод
    'print': print,
    # Исключения
    'Exception': Exception, 'ValueError': ValueError, 'TypeError': TypeError,
    'KeyError': KeyError, 'IndexError': IndexError, 'AttributeError': AttributeError,
    'RuntimeError': RuntimeError, 'StopIteration': StopIteration,
    'ZeroDivisionError': ZeroDivisionError, 'OverflowError': OverflowError,
    # Константы
    'True': True, 'False': False, 'None': None,
}

# 🛡️ НОВОЕ: Функция инициализации для жестких лимитов ОС
def limit_resources():
    """Устанавливает жесткие лимиты CPU и RAM для дочернего процесса."""
    # CPU time limit (soft, hard) in seconds. Sends SIGXCPU, then SIGKILL.
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_EXECUTION_TIME, MAX_EXECUTION_TIME + 2))
    # Virtual memory limit (1.5 GB) — нужно для растровых операций
    # (rasterio/GDAL читают окна в RAM; scipy/scikit-image метки связных областей)
    resource.setrlimit(resource.RLIMIT_AS, (int(1.5 * 1024 * 1024 * 1024), int(1.5 * 1024 * 1024 * 1024)))
    # Disable core dumps
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

# Инициализируем пул с применением лимитов к каждому воркеру
executor_pool = ProcessPoolExecutor(max_workers=2, initializer=limit_resources)

# Graceful shutdown
def cleanup():
    executor_pool.shutdown(wait=True)

atexit.register(cleanup)

# --- МОДЕЛИ ДАННЫХ ---
class CodeRequest(BaseModel):
    code: str
    context: dict = {}

class CodeResult(BaseModel):
    success: bool
    stdout: str
    stderr: str
    result: dict | list | str | int | float | None = None
    charts: list | None = None  # графики matplotlib: [{title, format, data_base64}]

# --- AST ФИЛЬТР (с блокировкой dunder и mro) ---
def validate_code(code: str) -> tuple[bool, str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    for node in ast.walk(tree):
        # Проверка импортов
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split('.')[0] in FORBIDDEN_NAMES:
                    return False, f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split('.')[0] in FORBIDDEN_NAMES:
                return False, f"Forbidden import: {node.module}"

        # Проверка вызовов функций
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_NAMES:
                return False, f"Forbidden function: {node.func.id}"

        # Проверка атрибутов (защита от dunder и mro обхода)
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS:
                return False, f"Forbidden attribute access: .{node.attr}"

        # Проверка имен переменных (включая dunder)
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                return False, f"Forbidden name usage: {node.id}"
            # Блокируем все dunder-методы, КРОМЕ __result__ и __charts__ (специальные переменные для возврата данных)
            if node.id.startswith('__') and node.id.endswith('__') and node.id not in ('__result__', '__charts__'):
                return False, f"Forbidden dunder method: {node.id}"

    return True, ""

# --- СБОР ГРАФИКОВ matplotlib (__charts__) ---
MAX_CHART_PNG_BYTES = 2 * 1024 * 1024  # 2MB на один PNG — защита от гигантских figure
CHART_DPI = 110

def _collect_charts(charts_spec) -> list | None:
    """Превращает __charts__ в список {title, format, data_base64}.

    Допустимые элементы __charts__:
      - str — заголовок, относящийся к следующему figure (или предыдущему, если он последний);
      - plt.Figure / объект с методом savefig — явно созданные фигуры;
      - dict {'title': str, 'fig': Figure} — фигура с заголовком.
    Если список пустой или None — забираем все открытые фигуры pyplot
    (plt.get_fignums()), чтобы работал простой вариант: plt.plot(...); plt.show().
    """
    import base64
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    items = []  # [(title, figure_or_None)]
    if charts_spec is None:
        nums = plt.get_fignums()
        items = [(f"figure_{n}", plt.figure(n)) for n in nums]
    elif isinstance(charts_spec, (list, tuple)):
        pending_title = None
        for el in charts_spec:
            if isinstance(el, str):
                pending_title = el
            elif hasattr(el, 'savefig'):  # matplotlib.figure.Figure
                items.append((pending_title or f"chart_{len(items)+1}", el))
                pending_title = None
            elif isinstance(el, dict) and hasattr(el.get('fig'), 'savefig'):
                items.append((el.get('title') or pending_title or f"chart_{len(items)+1}", el['fig']))
                pending_title = None
        # строка без последующей фигуры — подписываем последний figure из pyplot
        if pending_title and not items:
            nums = plt.get_fignums()
            if nums:
                items = [(pending_title, plt.figure(nums[-1]))]
    elif hasattr(charts_spec, 'savefig'):
        items = [("chart_1", charts_spec)]

    charts = []
    for title, fig in items:
        try:
            buf = io.BytesIO()
            fig.savefig(buf, format='png', dpi=CHART_DPI, bbox_inches='tight')
            png = buf.getvalue()
            if len(png) > MAX_CHART_PNG_BYTES:
                charts.append({"title": str(title), "format": "png",
                               "error": f"Image too large ({len(png)} bytes > {MAX_CHART_PNG_BYTES})"})
            else:
                charts.append({
                    "title": str(title),
                    "format": "png",
                    "data_base64": base64.b64encode(png).decode('ascii'),
                })
        except Exception as e:
            charts.append({"title": str(title), "format": "png",
                           "error": f"render failed: {type(e).__name__}: {e}"})
    # очищаем pyplot-фигуры, чтобы не копить RAM между вызовами в воркере
    try:
        plt.close('all')
    except Exception:
        pass
    return charts or None

# --- СИНХРОННОЕ ВЫПОЛНЕНИЕ ---
def execute_code_sync(code: str, context: dict) -> dict:
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    result = None

    safe_globals = {'__builtins__': SAFE_BUILTINS}

    # Автоматически импортируем разрешенные библиотеки
    try:
        import numpy as np
        import pandas as pd
        import geopandas as gpd
        from shapely.geometry import Point, LineString, Polygon, MultiPoint, MultiLineString, MultiPolygon, LinearRing, GeometryCollection, shape, mapping, box
        from shapely.ops import unary_union, transform, split, nearest_points, cascaded_union
        from shapely import wkt, wkb
        # --- Проекции (PROJ) ---
        import pyproj
        from pyproj import CRS, Transformer, Geod, Proj
        # --- Растры / форматы геоданных (GDAL) ---
        import rasterio
        from rasterio import open as raster_open
        from rasterio.enums import Resampling
        from rasterio.features import geometry_mask, rasterize
        from rasterio.warp import transform_bounds
        import rioxarray
        import xarray as xr
        from osgeo import gdal, ogr, osr
        # --- Научные вычисления / морфология растров ---
        import scipy
        from scipy import ndimage
        from scipy import stats as sp_stats
        from scipy import signal as sp_signal
        import skimage
        from skimage import measure as sk_measure, morphology as sk_morpho, filters as sk_filters
        # --- Графики и картограммы (рендер в PNG через base64) ---
        import matplotlib
        matplotlib.use('Agg')  # без GUI-бэкенда: только рендер в буфер/файл
        import matplotlib.pyplot as plt
        import matplotlib.cm as cm
        from matplotlib.colors import Normalize
        from matplotlib.patches import Patch
        from matplotlib.lines import Line2D
        # --- Облака точек LiDAR ---
        import laspy
        # --- Вспомогательные ---
        import mercantile

        safe_globals['np'] = np
        safe_globals['pd'] = pd
        safe_globals['gpd'] = gpd
        safe_globals['Point'] = Point
        safe_globals['LineString'] = LineString
        safe_globals['Polygon'] = Polygon
        safe_globals['MultiPoint'] = MultiPoint
        safe_globals['MultiLineString'] = MultiLineString
        safe_globals['MultiPolygon'] = MultiPolygon
        safe_globals['LinearRing'] = LinearRing
        safe_globals['GeometryCollection'] = GeometryCollection
        safe_globals['shape'] = shape
        safe_globals['mapping'] = mapping
        safe_globals['box'] = box
        safe_globals['unary_union'] = unary_union
        safe_globals['transform'] = transform
        safe_globals['split'] = split
        safe_globals['nearest_points'] = nearest_points
        safe_globals['wkt'] = wkt
        safe_globals['wkb'] = wkb
        # Проекции
        safe_globals['pyproj'] = pyproj
        safe_globals['CRS'] = CRS
        safe_globals['Transformer'] = Transformer
        safe_globals['Geod'] = Geod
        safe_globals['Proj'] = Proj
        # Растры / GDAL
        safe_globals['rasterio'] = rasterio
        safe_globals['raster_open'] = raster_open
        safe_globals['Resampling'] = Resampling
        safe_globals['geometry_mask'] = geometry_mask
        safe_globals['rasterize'] = rasterize
        safe_globals['transform_bounds'] = transform_bounds
        safe_globals['rioxarray'] = rioxarray
        safe_globals['xr'] = xr
        safe_globals['gdal'] = gdal
        safe_globals['ogr'] = ogr
        safe_globals['osr'] = osr
        # Научные вычисления / морфология
        safe_globals['scipy'] = scipy
        safe_globals['ndimage'] = ndimage
        safe_globals['sp_stats'] = sp_stats
        safe_globals['sp_signal'] = sp_signal
        safe_globals['skimage'] = skimage
        safe_globals['sk_measure'] = sk_measure
        safe_globals['sk_morpho'] = sk_morpho
        safe_globals['sk_filters'] = sk_filters
        # Графики / картограммы (PNG через __charts__)
        safe_globals['matplotlib'] = matplotlib
        safe_globals['plt'] = plt
        safe_globals['cm'] = cm
        safe_globals['Normalize'] = Normalize
        safe_globals['Patch'] = Patch
        safe_globals['Line2D'] = Line2D
        # LiDAR
        safe_globals['laspy'] = laspy
        safe_globals['mercantile'] = mercantile
    except ImportError as e:
        logger.warning(f"Failed to import geo libraries: {e}")

    safe_globals.update(context)

    try:
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            exec(code, safe_globals, safe_globals)
            result = safe_globals.get('__result__')
            charts = _collect_charts(safe_globals.get('__charts__'))

        return {
            "success": True,
            "stdout": stdout_capture.getvalue(),
            "stderr": stderr_capture.getvalue(),
            "result": result,
            "charts": charts,
        }
    except Exception as e:
        return {
            "success": False,
            "stdout": stdout_capture.getvalue(),
            "stderr": f"{type(e).__name__}: {e}",
            "result": None,
            "charts": None,
        }

# --- ASYNC ENDPOINT С ULTRA-HARDENED ЗАЩИТОЙ ---
@app.post("/execute", response_model=CodeResult)
@limiter.limit("10/minute")
async def execute_code(req: CodeRequest, request: Request):
    logger.info(f"[{datetime.now().isoformat()}] Received execution request.")
    
    # 🛡️ ПРАВИЛЬНАЯ проверка размера контекста (через сериализацию)
    try:
        context_bytes = json.dumps(req.context).encode('utf-8')
        if len(context_bytes) > MAX_CONTEXT_SIZE_MB * 1024 * 1024:
            return JSONResponse(
                status_code=400,
                content={"detail": f"Context size exceeds {MAX_CONTEXT_SIZE_MB}MB limit."}
            )
    except (TypeError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"detail": "Context is not JSON serializable."}
        )

    # 1. AST Валидация
    is_valid, error_msg = validate_code(req.code)
    if not is_valid:
        logger.warning(f"Security violation: {error_msg}")
        return CodeResult(success=False, stdout="", stderr=f"Security violation: {error_msg}", result=None)

    # 2. Выполнение в пуле процессов с таймаутом
    loop = asyncio.get_running_loop()
    try:
        coro = loop.run_in_executor(executor_pool, execute_code_sync, req.code, req.context)
        # +5 сек на overhead ОС
        res_dict = await asyncio.wait_for(coro, timeout=MAX_EXECUTION_TIME + 5)
        
        # 🛡️ ПРАВИЛЬНАЯ проверка размера результата (через сериализацию)
        if res_dict.get("result") is not None:
            result_bytes = json.dumps(res_dict["result"]).encode('utf-8')
            if len(result_bytes) > MAX_RESULT_SIZE_MB * 1024 * 1024:
                return CodeResult(
                    success=False,
                    stdout="",
                    stderr=f"Result size limit exceeded ({MAX_RESULT_SIZE_MB}MB).",
                    result=None
                )

        # 🛡️ Проверка суммарного размера графиков (base64 PNG)
        charts = res_dict.get("charts") or []
        if len(json.dumps(charts).encode('utf-8')) > MAX_RESULT_SIZE_MB * 1024 * 1024:
            res_dict["charts"] = [c for c in charts if "data_base64" not in c] or None
            logger.warning("Charts payload exceeded size limit — images dropped")

        return CodeResult(**res_dict)
        
    except asyncio.TimeoutError:
        # ОС уже убила зависший процесс через RLIMIT_CPU
        logger.error(f"Execution Timeout: Code exceeded {MAX_EXECUTION_TIME}s limit")
        return CodeResult(
            success=False,
            stdout="",
            stderr=f"Execution Timeout: Code exceeded {MAX_EXECUTION_TIME}s limit and was killed by OS.",
            result=None
        )
    except Exception as e:
        logger.error(f"Executor Error: {str(e)}")
        return CodeResult(
            success=False,
            stdout="",
            stderr=f"Executor Error: {str(e)}",
            result=None
        )

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/libs")
async def libs():
    """Диагностика: какие гео-библиотеки реально доступны в песочнице."""
    import importlib.metadata as md
    wanted = [
        "gdal", "rasterio", "pyproj", "geopandas", "shapely",
        "rioxarray", "xarray", "netCDF4", "h5py", "h5netcdf",
        "rio-cogeo", "pyogrio", "mercantile", "geopy",
        "scipy", "scikit-image", "matplotlib", "laspy", "laszip",
    ]
    versions = {}
    for name in wanted:
        try:
            versions[name] = md.version(name)
        except md.PackageNotFoundError:
            versions[name] = None
    # Проверка рантайм-импортов (то же, что видит код в песочнице)
    runtime = {}
    for mod in ["osgeo.gdal", "rasterio", "pyproj", "geopandas", "shapely",
                "rioxarray", "xarray", "netCDF4", "h5py", "mercantile",
                "scipy.ndimage", "skimage.measure", "matplotlib.pyplot", "laspy"]:
        try:
            import importlib
            importlib.import_module(mod)
            runtime[mod] = "ok"
        except Exception as e:
            runtime[mod] = f"error: {e}"
    return {"versions": versions, "imports": runtime}
