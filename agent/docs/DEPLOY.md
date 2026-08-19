# Деплой Джарвел Upgrade

Целевой хостинг — Railway. Конфиг уже лежит рядом с кодом: `agent/railway.toml`, `agent/.railwayignore`, `agent/.python-version` (3.13).

Production: [https://jarvel-web-production.up.railway.app/](https://jarvel-web-production.up.railway.app/).

Деплой в production — только после явного подтверждения и секретов в Dashboard, не из shell history.

## Сервис

Один сервис отдаёт FastAPI, Jinja, static и agent API.

| Параметр | Значение |
|---|---|
| Root Directory | `agent` |
| Entrypoint | `app.main:app` |
| Start | `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1` |
| Build | Railpack (`railway.toml`) |
| Healthcheck | `GET /readyz`, timeout 300 с |
| Restart | `ON_FAILURE`, до 10 попыток |
| Replicas / workers | **1 / 1** |

`--reload` на Railway не использовать. Несколько workers или autoscaling нельзя, пока `RunStore` и `ConversationStore` живут в памяти процесса: соседний воркер не увидит `run_id`, checkout сломается.

`.railwayignore` отсекает `.env`, `.venv`, кэши и `data/` с live-диагностикой.

## Секреты

В Railway Variables:

| Имя | Тип |
|---|---|
| `OPENROUTER_API_KEY` | sealed |
| `OPENROUTER_MODEL` | обычная |
| `OPENROUTER_PROVIDERS` | обычная, только vetted list |
| `OPENROUTER_TIMEOUT_SECONDS` | обычная |
| `AGENT_MAX_STEPS` | обычная |
| `TUTU_MCP_URL` | обычная |

`RAILWAY_TOKEN` в приложение не класть — это credential CLI/CI.

Локальный `.env` в образ не попадает. Перед выкладкой: secret scan, что ключ не зашит в исходниках и диагностических JSONL.

## Проверки после выкладки

1. `GET /readyz` → `{"status":"ready"}`.
2. `GET /healthz` → `ok` и небольшой `heartbeat_age_seconds`.
3. Главная отдаёт HTML и CSS.
4. Demo-поиск без checkout на staging. Живой `create_checkout_link` по умолчанию не дергать из монитора.

`/readyz` не зависит от временного 429 OpenRouter или Туту: это deploy-gate, не canary инвентаря.

Если процесс жив, но event loop завис, `/healthz` должен уйти в `503`. UI после рестарта честно говорит искать заново: in-memory подбор не восстанавливается.

## Эволюция

Следующий контур из плана — Postgres (runs/conversations/refs с TTL) + очередь + private worker. Тогда можно держать долгий агентный цикл без одного HTTP-response и переживать редеплой. До этого горизонтальное масштабирование запрещено.

Официальные ориентиры: [FastAPI на Railway](https://docs.railway.com/guides/fastapi), [healthchecks](https://docs.railway.com/deployments/healthchecks), [restart policy](https://docs.railway.com/deployments/restart-policy).
