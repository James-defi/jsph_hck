# HTTP API

База: тот же процесс, что отдаёт сайт. JSON и HTML-формы принимают одни и те же поля. Все ответы публичные: без `checkout_ref`, ключей и raw MCP.

Live-приложение: `uvicorn app.main:app`. Demo: `uvicorn app.web:app`.

## Страницы

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/` | Чат и глобус. Live — пустой composer; demo — сразу превью сценария |
| `POST` | `/search` | HTML-поиск (progressive enhancement, если JS выключен) |
| `POST` | `/concession/replan` | HTML-подтверждение уступки. В текущем чате кнопки нет |

Статика: `/static/styles.css`, `chat.js`, `globe.js`, `globe.css`.

## JSON

### `POST /api/search`

Тело:

```json
{
  "query": "Один человек, 21 августа 2026, самолёт VKO → LED",
  "conversation_id": "conv-…"
}
```

`conversation_id` опционален. Невалидный id сервер заменяет новым.

Успех: `200` и публичная презентация (`run_id`, `summary`, `contract`, `timeline`, `scenarios`, `rejection_summary`, `conversation_id`). У рекомендованных сценариев в `booking_units[].tariffs[]` есть `variant_id` без исходного MCP-ref.

Ошибки:

| Код | Когда |
|---|---|
| `422` | Пустой query или длиннее 6000 символов |
| `200` + refusal presentation | Высокоуверенная prompt injection (без LLM/MCP) |
| `503` | Сбой агента / апстрима; текст без traceback |

### `POST /api/checkout`

Тело — только то, что пользователь кликнул:

```json
{
  "run_id": "run-…",
  "component_ref": "common",
  "variant_id": "fare-…"
}
```

Успех: `{ "url", "handoff_kind", "message" }`. `handoff_kind` обычно `checkout_deeplink` или `search_redirect`. URL открывать как есть, не обрезать.

| Код | Когда |
|---|---|
| `422` | Нет выбора, чужой/устаревший run, тариф не recommended |
| `503` | MCP не собрал ссылку; бронь не создавалась |

Клиент (`static/chat.js`) открывает `url` через `window.open`.

### `POST /api/concession/replan`

```json
{
  "run_id": "run-…",
  "proposed_max_wait_minutes": 340,
  "conversation_id": "conv-…"
}
```

Сервер сверяет минуты с уже сохранённым proposal. Подменить хаб, дату или нижний порог этим запросом нельзя.

### `POST /api/transcribe`

Диктовка в поле запроса, **поиск не запускает**.

```json
{
  "audio_base64": "…",
  "format": "webm"
}
```

Ответ: `{ "text": "…" }`. Форматы: wav, mp3, flac, m4a, ogg, webm, aac, mp4. Лимит размера — `OPENROUTER_STT_MAX_AUDIO_BYTES` (по умолчанию 4 МБ). Без ключа OpenRouter — `503`.

## Здоровье

| Путь | Смысл |
|---|---|
| `GET /health` | Короткий ok + имя сервиса |
| `GET /healthz` | Liveness: heartbeat event loop. Если петля клинит дольше ~5 с — `503` |
| `GET /readyz` | Readiness: у сервиса есть `run` / `create_checkout_link` / `replan_concession`. **Не** зовёт OpenRouter и Tutu |

Railway healthcheck смотрит `/readyz`. Мониторинг живого процесса — `/healthz`.

## Инварианты для клиента

- Не слать паспорт, карту, `checkout_ref`, API-ключи.
- После нового поиска не использовать старые `run_id` и ссылки.
- Фраза в чате «да, покупай» не вызывает checkout. Нужен клик по тарифу с `variant_id`.
- Рестарт процесса обнуляет RunStore: «подбор перезапущен, ищите заново».
