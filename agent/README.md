# Джарвел Upgrade — сервис агента

Рабочая папка прототипа. Продуктовое имя — **Джарвел Upgrade**; в коде модули ещё называются SpeakFare / GroupSync. Это не личный desktop-ассистент: нет файлов, shell, браузера, почты, календаря и долгой памяти пользователя.

Пользовательский вход — корневой [README](../README.md). Здесь — запуск, конфигурация и указатели в техническую документацию.

## Запуск

Python 3.13 (см. `.python-version`). Из этой папки:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

В `.env` нужен только `OPENROUTER_API_KEY`. Файл в Git не коммитится.

| Режим | Команда | Назначение |
|---|---|---|
| Live | `uvicorn app.main:app --reload` | OpenRouter + Tutu MCP, точка входа `app.main:app` |
| Demo UI | `uvicorn app.web:app --reload` | `DemoGroupSyncService`, без ключа и сети |

Сайт локально: `http://127.0.0.1:8000/`. Production: [jarvel-web-production.up.railway.app](https://jarvel-web-production.up.railway.app/). Health: `GET /healthz`, readiness: `GET /readyz`.

Минимальный запрос:

```text
Нас трое: Аня из VKO, Илья из LED и Саша из SVX. 2026-08-21 встречаемся в IST,
ждём 2–5 часов и затем летим одним рейсом в LHR. Багаж не нужен.
```

## Что делает runtime

- Модель `deepseek/deepseek-v4-flash-0731` через OpenRouter. Провайдеры только из allow-list: Baseten, затем DeepInfra, Fireworks, StreamLake. У всех должны быть reasoning, tools и `tool_choice`.
- Модели видны **два** tool: `plan_group_sync` и `plan_individual_trip`. Оба терминальные: успешный вызов заканчивает ход. `search_*` и `create_checkout_link` модели недоступны.
- Python нормализует офферы, прогоняет solver и SafetyGate, кладёт `checkout_ref` только в серверный `RunStore`.
- Диктовка — отдельный STT-контур OpenRouter (`whisper` / `chirp`), не тот же provider allow-list, что у чата.
- Retry 429/5xx — только у запроса к модели и только **до** исполнения tools. Вызовы Tutu и checkout не повторяются автоматом.

Подробности: [ARCHITECTURE.md](docs/ARCHITECTURE.md), [API.md](docs/API.md).

## Тесты

```powershell
python -m pytest -p no:xonsh tests -q -p no:cacheprovider
```

Тесты не ходят в сеть и не читают ключ. Они покрывают агентный цикл (`ScriptedLLM` + `FakeTutuMcpClient`), SafetyGate, prompt-injection, checkout только после выбора тарифа, число пассажиров, solver, conversation и UI.

Live-диагностика **не** является CI-gate. Из корня репозитория:

```powershell
python agent/scripts/run_live_diagnostic.py --timeout-seconds 900 --create-handoff
python agent/scripts/run_live_corpus.py --timeout-seconds 300
```

Лимит 15 минут, бронь не создаётся. JSONL пишется в `agent/data/diagnostics/` (папка в Git не попадает). В лог не должны попадать ключ OpenRouter, hidden reasoning, `checkout_ref`, сырой MCP и полная handoff-ссылка.

## Документация

| Файл | Содержание |
|---|---|
| [docs/README.md](docs/README.md) | Индекс |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | Как пользоваться экраном |
| [docs/FAQ.md](docs/FAQ.md) | Частые вопросы |
| [docs/SAFETY_AND_LIMITS.md](docs/SAFETY_AND_LIMITS.md) | Что агент не делает |
| [docs/GLOSSARY.md](docs/GLOSSARY.md) | Термины |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Как устроен код |
| [docs/API.md](docs/API.md) | HTTP-контракт |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Railway |
| [PLAN.md](PLAN.md) | План прототипа и acceptance gates |
| [AGENT_MIND_MAP.md](AGENT_MIND_MAP.md) | Ранняя карта потока; актуальная схема — в архитектуре |

## Переменные окружения

См. `.env.example`. Главные:

| Переменная | Смысл |
|---|---|
| `OPENROUTER_API_KEY` | Секрет. Только `.env` / Railway Variables |
| `OPENROUTER_MODEL` | По умолчанию `deepseek/deepseek-v4-flash-0731` |
| `OPENROUTER_PROVIDERS` | Строгий allow-list, не произвольный роутинг |
| `TUTU_MCP_URL` | По умолчанию `https://mcp.tutu.ru/mcp` |
| `AGENT_MAX_STEPS` | Бюджет агентного цикла, по умолчанию 20 |
