# Документация Джарвел Upgrade

Два слоя одной документации: как пользоваться прототипом и как он устроен. Интерфейс ещё меняется параллельными агентами; поведение safety/checkout описано по текущему коду в `agent/app`.

## Пользовательская

| Документ | Зачем |
|---|---|
| [USER_GUIDE.md](USER_GUIDE.md) | Экран, примеры запросов, как выбрать тариф |
| [FAQ.md](FAQ.md) | Цена, ссылка на поиск, пустой результат, новый подбор |
| [SAFETY_AND_LIMITS.md](SAFETY_AND_LIMITS.md) | Отказы, запас на стыковку, что не вводить |
| [GLOSSARY.md](GLOSSARY.md) | Хаб, self-transfer, search redirect, договор |

Старт для человека, который открыл сайт: [USER_GUIDE](USER_GUIDE.md). Для жюри достаточно корневого [README](../../README.md).

## Техническая

| Документ | Зачем |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Слои, trust boundary, solver, RunStore |
| [API.md](API.md) | Маршруты FastAPI |
| [DEPLOY.md](DEPLOY.md) | Railway, healthchecks, секреты |
| [../README.md](../README.md) | Запуск и тесты из папки `agent` |
| [../PLAN.md](../PLAN.md) | Исходный план MVP и gates |
| [../../tutu-mcp.md](../../tutu-mcp.md) | Внешний инвентарь Туту |

Карта ранней версии агентного цикла: [AGENT_MIND_MAP.md](../AGENT_MIND_MAP.md). Она расходится с текущим allow-list tools — ориентир по коду в [ARCHITECTURE.md](ARCHITECTURE.md).
