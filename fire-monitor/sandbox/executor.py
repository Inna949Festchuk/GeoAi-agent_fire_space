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
    # Virtual memory limit (512 MB)
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
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
            # Блокируем все dunder-методы, КРОМЕ __result__ (специальная переменная для возврата данных)
            if node.id.startswith('__') and node.id.endswith('__') and node.id != '__result__':
                return False, f"Forbidden dunder method: {node.id}"

    return True, ""

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
        from shapely.geometry import Point, LineString, Polygon, MultiPoint, MultiLineString, MultiPolygon, shape, mapping, box
        from shapely.ops import unary_union, transform
        from shapely import wkt
        
        safe_globals['np'] = np
        safe_globals['pd'] = pd
        safe_globals['gpd'] = gpd
        safe_globals['Point'] = Point
        safe_globals['LineString'] = LineString
        safe_globals['Polygon'] = Polygon
        safe_globals['MultiPoint'] = MultiPoint
        safe_globals['MultiLineString'] = MultiLineString
        safe_globals['MultiPolygon'] = MultiPolygon
        safe_globals['shape'] = shape
        safe_globals['mapping'] = mapping
        safe_globals['box'] = box
        safe_globals['unary_union'] = unary_union
        safe_globals['transform'] = transform
        safe_globals['wkt'] = wkt
    except ImportError as e:
        logger.warning(f"Failed to import geo libraries: {e}")

    safe_globals.update(context)

    try:
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            exec(code, safe_globals, safe_globals)
            result = safe_globals.get('__result__')

        return {
            "success": True,
            "stdout": stdout_capture.getvalue(),
            "stderr": stderr_capture.getvalue(),
            "result": result
        }
    except Exception as e:
        return {
            "success": False,
            "stdout": stdout_capture.getvalue(),
            "stderr": f"{type(e).__name__}: {e}",
            "result": None
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
