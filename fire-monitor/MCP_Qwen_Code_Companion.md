# Настройка MCP в Qwen Code Companion

### Шаг 1: Создайте файл конфигурации MCP

В корне вашего проекта создайте файл **`.qwen/mcp.json`** (именно в папке `.qwen`, а не `.vscode`):

```bash
mkdir -p .qwen
touch .qwen/mcp.json
```

### Шаг 2: Добавьте конфигурацию

Откройте `.qwen/mcp.json` и вставьте:

```json
{
  "mcpServers": {
    "fire-monitor": {
      "type": "sse",
      "url": "http://localhost:8001/mcp"
    }
  }
}
```

### Шаг 3: Перезапустите VSCode

Закройте и снова откройте VSCode, чтобы расширение подхватило конфигурацию.

---

## ✅ Проверка подключения

1. **Убедитесь, что ваш проект запущен:**
   ```bash
   docker compose -f docker-compose.dev.yml up
   ```

2. **Откройте панель Qwen Code Companion** (обычно иконка в боковой панели или через `Ctrl+Shift+P` → "Qwen Code Companion")

3. **Проверьте список инструментов:**
   В чате Qwen Code Companion напишите:
   ```
   /tools
   ```
   
   Должен появиться список, включая:
   - `search_fires`
   - `search_sentinel2`
   - `get_fire_stats`
   - `start_burn_mapping`
   - `run_validation`
   - и другие

---

## 💬 Использование в чате

Теперь вы можете писать запросы прямо в чате Qwen Code Companion:

**Пример 1: Простой запрос**
```
Используй инструмент search_fires для поиска пожаров в bbox 80,55,110,70 за последние 3 дня
```

**Пример 2: Статистика**
```
Вызови get_fire_stats для Сибири (bbox 80,55,110,70) и покажи распределение по источникам
```

**Пример 3: Поиск спутниковых снимков**
```
Найди Sentinel-2 сцены для Байкала (bbox 103,51,110,56) за период с 2025-08-01 по 2025-08-15
```

**Пример 4: Валидация данных «Стрелы»**
```
Запусти run_validation для Иркутской области (bbox 97,51,119,60) за июль 2024
(start_date=2024-07-01, end_date=2024-07-31) — нужен Precision/Recall/F1
и IoU гарей по Рослесхозу и NASA MCD64A1
```
Первый запуск скачивает тайлы MCD64A1 с NASA и занимает несколько минут.
Отчёты: `reports/validation_report.md`, `validation_map.html`, `metrics_summary.csv`.

---

## 🔍 Troubleshooting

**Если инструменты не появились:**

1. Проверьте, что файл находится именно в `.qwen/mcp.json` (не `.vscode`)
2. Убедитесь, что MCP-сервер запущен:
   ```bash
   curl http://localhost:8001/mcp
   ```
3. Перезагрузите окно VSCode: `Ctrl+Shift+P` → "Developer: Reload Window"

**Если подключение не работает:**

Проверьте логи Qwen Code Companion:
- `Ctrl+Shift+U` → выберите "Qwen Code Companion" в выпадающем списке
- Или посмотрите Output Panel

---

## 📊 Альтернативный вариант: через настройки VSCode

Если файл `.qwen/mcp.json` не работает, попробуйте добавить в `settings.json` VSCode:

1. `Ctrl+,` → иконка "Open Settings (JSON)"
2. Добавьте:

```json
{
  "qwenCodeCompanion.mcpServers": {
    "fire-monitor": {
      "type": "sse",
      "url": "http://localhost:8001/mcp"
    }
  }
}
```

Попробуйте сначала вариант с `.qwen/mcp.json` — это стандартный способ для Qwen Code Companion.