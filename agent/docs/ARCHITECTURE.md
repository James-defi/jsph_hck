# Архитектура Джарвел Upgrade

Снимок реализации в `agent/app` на момент написания. Продуктовые намерения и ещё не закрытые gates — в [PLAN.md](../PLAN.md). HTTP — в [API.md](API.md).

## Принцип

LLM выбирает *когда* планировать поездку. Python решает *что можно показать* и *можно ли отдать ссылку на Туту*.

```text
Browser (чат + глобус)
    → FastAPI web.py
        → InputSecurityGate
        → LiveGroupAgent / DemoGroupSyncService
            → AgentRuntime + OpenRouter
            → plan_group_sync | plan_individual_trip
                → Tutu MCP (instructions + search)
                → station_resolve / geo
                → GroupSyncSolver
                → SafetyGate
                → presentation
                → RunStore  (checkout_ref только здесь)
        → public JSON / HTML без секретов
    → клик по тарифу → POST /api/checkout
        → claim_variant + checkout_handoff_guard
        → MCP create_checkout_link
        → URL tutu.ru в новой вкладке
```

Точка сборки live-приложения — `app.main:app`. Она инжектит `build_live_service()` и опциональный STT. Demo-стенд — `create_app(DemoGroupSyncService())` в `app.web`.

## Слои и файлы

| Слой | Модуль | Роль |
|---|---|---|
| HTTP / UI | `web.py`, `templates/`, `static/` | Формы, JSON API, глобус, redaction ответа |
| Конфиг | `config.py` | `.env`, allow-list провайдеров, лимиты |
| Безопасность входа | `security.py` | NFKC, zero-width, 6000 символов, injection refusal |
| Агентный цикл | `runtime.py`, `models.py`, `tool_registry.py` | Шаги, trace без hidden reasoning |
| LLM | `openrouter.py` | Chat completions, retry до tools |
| STT | `transcribe.py` | Whisper/Chirp, не чатовый provider list |
| Планирование | `application.py` | `PlanningSession`, два model-visible tool |
| MCP | `tutu_mcp.py` | Streamable HTTP JSON-RPC/SSE |
| Точки | `station_resolve.py`, `geo.py` | Станции из инструкций Туту, совпадение точек встречи, пины глобуса |
| Факты маршрута | `solver.py` | Хаб, окно, подпись общего сегмента |
| Рекомендация | `safety.py` | `blocked` / `needs_verification` / `caution` / `recommended` |
| Карточки | `presentation.py` | Договор, timeline, сценарии, тарифы |
| Уступка | `constraint_negotiator.py` | Только увеличение `max_wait`, без ослабления пола |
| Состояние | `service.py`, `conversation.py` | `RunStore`, tab-scoped история |
| Checkout | `service.py` + `solver.checkout_handoff_guard` | Byte-for-byte исходный ref |

## Что видит модель

Production-реестр в `LiveGroupAgent._registry` содержит только:

- `plan_group_sync` — группа, хаб, общее плечо, фидеры;
- `plan_individual_trip` — один человек, A→B, `avia` / `rail` / `bus`.

У обоих `additionalProperties: false` и `finishes_agent_run=True`. После успеха runtime не даёт модели второй ход «на всякий случай поискать ещё».

Внутри `PlanningSession` Python сам вызывает `get_*_instructions` и `search_*`. Эти tools **не** попадают в OpenAI schema. `create_checkout_link` тоже не в schema: его дергает только `/api/checkout` после клика.

Системный промпт (`LIVE_SYSTEM_PROMPT`) запрещает обещать бронь, раскрывать ключи и обходить policy. Это не граница защиты: полномочия дают схемы, solver, SafetyGate и RunStore.

## Trust boundary

| Источник | Статус |
|---|---|
| Pydantic-схемы, solver, SafetyGate, RunStore | trusted |
| Текст пользователя, история вкладки | untrusted |
| Tool call модели | untrusted proposal, валидируется до handler |
| Ответ Tutu MCP | untrusted external data, нормализуется |
| `OPENROUTER_API_KEY`, `checkout_ref`, private recipe | private, не в UI/trace/логах |

`InputSecurityGate` — ранний фильтр. Высокоуверенная инъекция возвращает фиксированный отказ без LLM и MCP.

Рекурсивная redaction убирает `checkout_ref`, URL с query, ключи, tokens, `private_recipe`, `concession_replan_context`.

## Solver и SafetyGate

`GroupSyncSolver` отсекает комбинации, которые нельзя честно показать: не тот хаб, ожидание вне `[min_wait, max_wait]`, обязательный багаж не подтверждён, нет времён сегментов. Общее плечо у авиа — строго один прямой рейс; наземный билет может включать пересадку внутри одного билета, поэтому у поезда и автобуса проверяются первая и последняя точки маршрута.

Когда сценариев нет, `presentation._meeting_window_detail` объясняет причину по участникам, а не общей фразой: кто именно приезжает после ухода последнего общего рейса или на сколько ближайшая стыковка выходит за максимум ожидания.

Оставшиеся комбинации ранжируются, и в чат уходит **один** вариант — тот, что прошёл проверки и ранжирование первым. Прошлые ярлыки «дешевле / баланс / спокойнее» из выдачи убраны.

Если `max_wait_minutes` пришёл нулевым (схема требует поле, а пользователь окно не называл), `plan_group_sync` подставляет `DEFAULT_MAX_MEETING_WAIT_MINUTES = 240`. Нижняя граница безопасности так не меняется.

### Совпадение точек встречи

Хаб и финиш в договоре записаны так, как их назвал пользователь («Санкт-Петербург»), а Туту возвращает станции и остановки («Санкт-Петербург — Ладожский вокзал (2004004)», «Выборг, 2004682», «Автовокзал»). Сопоставлением занимается `geo.meeting_point_matches`:

| Вид транспорта | Правило |
|---|---|
| Авиа | строгое сравнение кодов, IST ≠ SAW; название рейса не смягчает правило |
| Поезд, автобус | сначала код, затем **название** конечной точки: город из строки вида `Город, id`, дальше алиасы и координаты |

Для автобуса сегмент часто называет только остановку, поэтому город берётся с уровня оффера (`checkout_ref.city_from` / `city_to`) через `TravelOffer.origin_city` / `destination_city`. Те же названия попадают в `to_dict()`, поэтому `safety.py` и `constraint_negotiator.py` перепроверяют стыковку по тем же данным, а не по одному коду.

Незнакомый id станции больше не ломает поиск: город берётся из названия. Справочник городов в `geo.py` дополнен наземными пинами (Тверь, Владимир, Псков, Петрозаводск и другие) — они нужны глобусу там, где аэропорта нет.

`RecommendationPolicy` / `SafetyGate` стоят между solver и UI и повторно на checkout:

| Вердикт | CTA / тарифная ссылка |
|---|---|
| `recommended` | можно, если есть exact variant |
| `caution` | в первой версии основного CTA нет |
| `needs_verification` | нет |
| `blocked` | нет |

Полы самостоятельной пересадки, пока MCP не подтвердил единый защищённый билет:

| Стыковка | Минимум |
|---|---:|
| Самолёт ↔ самолёт / поезд / автобус | 240 мин |
| Поезд → поезд, та же станция | 30 мин |
| Автобус → автобус, та же точка | 45 мин |
| Разные аэропорты / вокзалы без перехода | отказ или «нужно проверить» |

Совпадение перевозчика само по себе не доказательство сквозного багажа.

## Глобус

`static/globe.js` рисует только то, что пришло в `timeline`: у каждого плеча есть `mode` и координаты `origin` / `destination` из `geo.py`. Вид транспорта определяет геометрию:

- `avia` — дуга с подъёмом над поверхностью и иконка самолёта;
- `rail` и `bus` — линия по поверхности (подъём 0.006 радиуса, минимальный боковой изгиб), иконка поезда или автобуса, скрытая на обратной стороне шара.

Поэтому поезд и автобус идут по земле, а не летят. Если координаты точки не разошлись, плечо на глобусе не рисуется — сначала чинится `geo.py`, а не фронтенд.

`templates/index.html` подключает статику с версией в query (`chat.js?v=…`, `globe.js?v=…`). После правки этих файлов версию нужно поднять, иначе браузер отдаст закешированную старую сцену.

## RunStore и checkout

Браузер шлёт только `run_id`, `component_ref`, `variant_id`. Сервер:

1. Находит run и вариант.
2. Берёт короткую checkout-lease (гонка с concession replan запрещена).
3. Проверяет `checkout_handoff_guard`: явный выбор, исходный ref, пассажиры.
4. Вызывает MCP `create_checkout_link` с ref **как пришёл от Туту**.
5. Отдаёт URL. Клиент открывает его в новой вкладке.

`ObservedTutuInventory` помнит только офферы, реально увиденные в этом run. Подставить чужой hash из промпта нельзя.

Состояние process-local: рестарт стирает подборы. Поэтому в деплое один worker и одна replica, пока нет Postgres. См. [DEPLOY.md](DEPLOY.md).

## Диалог

`ConversationStore` живёт в памяти процесса. Id вкладки — hidden field `conversation_id` вида `conv-…`, не cookie. Лимиты: 8 реплик, TTL 2 часа, максимум 500 разговоров. Security-отказы в историю не пишутся. Обновление страницы начинает новый диалог.

## «Цена одной уступки»

Чистая функция `suggest_one_max_wait_concession` предлагает поднять **только** верхнюю границу ожидания, если baseline дал ноль сценариев, а повторный solver на тех же MCP-снимках находит `pass`.

Подтверждение: `POST /api/concession/replan` с `run_id` и уже показанным максимумом. LLM в этом пути нет. Lifecycle: `active → replanning → superseded`. При ошибке поиска старый run возвращается в `active`.

Текущий чат **не рисует** карточку «Подтвердить и пересчитать» (это проверяет `test_web.py`). API и solver остаются; пользователь пока меняет окно ожидания новым текстом запроса.

## OpenRouter

- Чат: `provider.order` + `provider.only` = vetted list.
- Retry: 429/5xx/transport, экспоненциальный backoff, только до tool execution.
- `reasoning` / `reasoning_details` возвращаются модели на следующем шаге и исключены из `model_dump` / UI trace.
- STT: отдельная цепочка моделей, лимит ~4 МБ аудио.

## Тесты как контракт

Offline-контур: `ScriptedLLM` + `FakeTutuMcpClient` + FastAPI `TestClient`. Нет `sleep` как acceptance-лимита.

`tests/test_full_cycle.py` гоняет solo/group и смешанные виды транспорта из PLAN §4.2. Парный negative-case: короткий буфер не даёт CTA и даёт `422` на поддельный checkout.

Ещё: `test_security.py` (injection), `test_safety.py`, `test_solver.py`, `test_application.py`, `test_individual.py`, `test_web.py`, `test_conversation.py`, `test_transcribe.py`, `test_openrouter.py`, `test_runtime.py`, `test_constraint_negotiator.py`, `test_tutu_mcp.py`, `test_geo.py`, `test_station_resolve.py`.

Playwright в репозитории нет: HTML проверяется TestClient.
