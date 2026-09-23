# Pull Request: Multi-turn контекст разговора + автодиспетчер маршрутов

**Branch:** `qwen-code-49fa1773-57fe-4342-a1bc-1fce2737585e` → `main`
**Commits:** 5 (`b0f003e..028fac1`) · **Изменено:** 9 файлов, +383 / −41

---

## Описание

PR решает три связанные проблемы геоагента:

1. **Агент не помнит контекст разговора.** На запрос «построй линию долёта от базы, галсы и возврат» после предыдущих сообщений агент отвечал «у меня нет контекста предыдущего разговора». Теперь история диалога (последние 20 реплик user/assistant) передаётся в LLM вместе с каждым новым сообщением.

2. **Сложные цепочки «пожары → части → маршруты» разваливались.** LLM получал от инструментов только summary без координат и пытался извлекать их через `execute_python`, сжёг до 16 итераций и так и не строил маршруты. Добавлен инструмент-автодиспетчер `dispatch_routes_to_fires`, выполняющий всю цепочку за один вызов.

3. **Не было наблюдаемости.** Добавлено логирование каждого вызванного инструмента и итога `actions`, чтобы по `docker compose logs backend | grep '[chat]'` было видно, что использовалось — sandbox, MCP или прямые вызовы.

---

## Изменения

### 1. Multi-turn контекст разговора (commit `028fac1`)

| Файл | Изменение |
|---|---|
| `backend/geo_processing/chat_handler.py` | `handle_chat_message(..., history=[])`; новая `_build_messages()`: system prompt + последние `MAX_HISTORY_MESSAGES=20` реплик user/assistant + текущее сообщение с bbox. Невалидные роли и пустые сообщения отбрасываются. |
| `backend/fires/serializers.py` | `ChatMessageSerializer` дополнен полем `history` (`ChatHistoryMessageSerializer`, many=True, required=False). |
| `backend/fires/views.py` | `chat_view` читает `history` из запроса и передаёт в `handle_chat_message`. |
| `frontend/src/api/client.js` | `sendMessage(message, bbox, history)` отправляет историю в теле `POST /api/chat/`. |
| `frontend/src/components/ChatPanel.jsx` | Перед отправкой формируется история из состояния `messages` (user/assistant, без приветствия и текущего сообщения, до 20 последних). |

### 2. Инструмент `dispatch_routes_to_fires` + фикс слоёв (commit `ebc159`)

- `backend/geo_processing/chat_handler.py`: новый инструмент — кластеризация точек пожаров по haversine (`cluster_km`, по умолч. 5 км), поиск ближайшей пожарной части для каждого очага (Overpass), пакетное построение автомобильных маршрутов (OSRM `build_routes_batch`), возврат объединённого GeoJSON (маршруты + станции). Аргумент `fires_geojson` подставляется автоматически из результата предыдущего `search_fires` — модели не нужно копировать данные.
- Ключевые инструменты (`build_route`, `build_routes_batch`, `find_nearest_fire_stations`) теперь возвращают полный GeoJSON через `map_data`, доступный в контексте LLM; большой GeoJSON сокращается в логах/actions (`_args_for_log`).
- `SYSTEM_PROMPT`: явный workflow «нашёл пожары → вызови dispatch_routes_to_fires без аргументов», запрет на извлечение координат через `execute_python`.
- `frontend/src/App.jsx`: устойчивая обработка `map_data_list` — новые типы слоёв `'routes'`, `'fire_stations'` корректно попадают в `customData` и отображаются на карте (раньше терялись).

### 3. Логирование (commits `b0f003e`, `35c11be`)

- `chat_handler.py`: `logger.info('[chat] iteration=N tool=... args=...')` на каждый вызов инструмента и `[chat] done: N action(s): ...` в конце.
- `backend/config/settings.py`: секция `LOGGING` для модулей `geo_processing`/`fires` (уровень INFO в stderr), иначе dev-сервер Django глушил логи.

### 4. Документация (commit `7dd5b29`)

- `README.md`: раздел «AI чат через API» с примером `history`, changelog v0.0.10, полный список 19 инструментов агента с тестовыми промтами.

---

## Как проверить

```bash
docker compose up -d --build backend frontend
docker compose logs -f backend | grep '\[chat\]'
```

1. **Контекст:** в чате последовательно: «Найди пожары возле Москвы» → «Построй линию долёта от ближайшей пожарной части к очагу, галсы над ним и возврат на базу». Агент должен продолжить, не переспрашивая координаты.
2. **Диспетчер:** «Найди пожары возле Москвы и построй оптимальные маршруты от пожарных частей к пожарам». Ожидаемые логи:
   ```
   [chat] iteration=1 tool=search_fires ...
   [chat] iteration=2 tool=dispatch_routes_to_fires args={'fires_geojson': '<FeatureCollection: N features>', ...}
   [chat] done: ... dispatch_routes_to_fires ...
   ```
   На карте — оранжевые линии маршрутов и синие точки частей.
3. **Sandbox vs прямой вызов:** `execute_python` появляется в логах только при реальном использовании sandbox (`fire-monitor-sandbox POST /execute`); `build_route`/`dispatch` идут напрямую в OSRM/Overpass.

## Тестирование

- `python3 -m py_compile` — все Python-файлы OK; `esbuild` JSX — OK.
- Юнит-тест `_build_messages`: история встраивается после system, невалидные роли отбрасываются, лимит 20 сообщений работает.
- Интегральный тест `_dispatch_routes_to_fires` на реальных Overpass/OSRM (4 точки из логов): 2 очага, 2 построенных маршрута, GeoJSON содержит route-линии и fire_station-точки; без `search_fires` в истории — понятная ошибка-подсказка для LLM.

## Обратная совместимость

- Поле `history` необязательное: старые клиенты шлют только `message`+`bbox` — поведение как раньше.
- Формат ответа `/api/chat/` не меняется (`actions` дополняется сокращённым отображением больших аргументов).
- Новые типы слоёв на фронтенде обрабатываются существующей веткой `customData`.
