# Tutu MCP

Живой инвентарь Туту для ИИ-агента. Решение на хакатоне строится вокруг него: LLM понимает фото и свободный текст, MCP ищет билеты и отели и отдаёт ссылку на оплату.

- **URL:** `https://mcp.tutu.ru/mcp`
- **Транспорт:** remote Streamable HTTP, без авторизации
- **Сервер:** `tutu-mcp-server` 0.38.0
- **Протокол:** MCP `2025-03-26`
- **Сайт-лендинг:** [mcp.tutu.ru/mcp](https://mcp.tutu.ru/mcp)

Подключение (примеры с лендинга):

```bash
claude mcp add --transport http tutu https://mcp.tutu.ru/mcp
```

```json
{
  "mcp": {
    "tutu": {
      "type": "remote",
      "url": "https://mcp.tutu.ru/mcp",
      "enabled": true
    }
  }
}
```

---

## Что это даёт

Поиск по пяти продуктам плюс мультимодал:

- авиа
- ЖД
- автобусы
- электрички
- отели (включая отзывы в деталях)

Сервер **read-only**. Платежей, аккаунтов и брони на стороне MCP нет. `create_checkout_link` только собирает URL; корзину создаёт браузер пользователя на tutu.ru. Финальные данные пассажиров и оплату человек подтверждает сам.

---

## Поток

1. Если домен ещё не использовали в сессии — один раз `get_<domain>_instructions`.
2. Поиск: `search_hotels` / `search_avia` / `search_rail` / `search_bus` / `search_etrain` / `search_multitransport`.
3. Карточка: `get_offer_details`. Для поездов схема мест — `get_rail_seatmap`.
4. Оплата: `create_checkout_link` с `checkout_ref` оффера → `{checkout_url, kind}`.

Пока пользователь сравнивает авиа — показывать только `search_results_url`, не звать checkout раньше выбора рейса.

Алиасы: `from_city`/`to_city` = `origin`/`destination`; у отелей `checkin_date`/`checkout_date`.

---

## Tools (16)

| Tool | Роль |
|---|---|
| `search_avia` | Авиа. Город или аэропорт/IATA (`LHR`, `IST`, «Хитроу»). |
| `search_hotels` | Отели по городу и датам. |
| `search_rail` | РЖД. |
| `search_bus` | Междугородние автобусы. |
| `search_etrain` | Электрички. |
| `search_multitransport` | Авиа + ЖД + автобус + электричка одним вызовом. |
| `get_offer_details` | Детали оффера: номера, тарифы, отзывы, условия. |
| `get_rail_seatmap` | Схема вагона и места. |
| `create_checkout_link` | Единый handle checkout для всех продуктов. |
| `get_avia_instructions` | Плейбук авиа. |
| `get_rail_instructions` | Плейбук ЖД. |
| `get_bus_instructions` | Плейбук автобусов. |
| `get_etrain_instructions` | Плейбук электричек. |
| `get_hotels_instructions` | Плейбук отелей. |
| `get_multitransport_instructions` | Плейбук мультимодала. |
| `fetch_resource` | Чтение `tutu://*` если клиент сам ресурсы не подставляет. |

### `search_avia` — поля

`origin`, `destination`, `departure_date`, `return_date`, `adults`, `children`, `infants`, `service_class`, `page`, `page_size`, `sort`, `price_max`, `direct_only`, `carriers`, `flight_numbers`, `view`.

`origin`/`destination` принимают город, имя аэропорта или IATA. Голый `IST` = только новый аэропорт Стамбула (SAW отсекается). «Стамбул» = все аэропорты города.

### `search_hotels` — поля

`city_name` или `geo_id`, `check_in`/`check_out`, `adults`, `children_ages`, `stars`, `price_max`, `meals`, `hotel_types`, `min_rating`, `free_cancellation`, `breakfast_included`, `hotel_amenities`, `room_amenities`, пагинация, `view`.

### Общие фильтры транспорта

На всех `search_*`: `price_max`, `direct_only`, `carriers`.  
Сортировка: `price_asc` (дефолт) / `price_desc` / `duration_asc` / `departure_asc`.  
У `search_multitransport` вместо sort — `optimize_for: 'price'|'time'`.

Перевозчика не угадывать латиницей. Сначала `meta.carriers_available` (`name`, `offers_count`, `price_from`), фильтровать точным `name` («Аэрофлот», не `aeroflot`). Что отсеял фильтр — в `meta.post_filter_dropped_*`.

Пагинация: `page` 1–10, `page_size` 1–30, дефолт 10. Смотреть `meta.has_more` и `meta.total_matched`.

`view`: `compact` (дефолт, выбор между вариантами) или `full`. Для отелей ещё `rules` (лестница отмены) и `reviews` (цитаты гостей).

---

## Resources

Читать через `fetch_resource`, если клиент их не инжектит.

| URI | Содержание |
|---|---|
| `tutu://help/overview` | Обзор и индекс. |
| `tutu://geo` | Справочник городов/точек. |
| `tutu://amenities/dictionary` | Коды удобств → русские подписи (автобус/ЖД/электричка). |
| `tutu://status` | Здоровье сервера и апстримов. |
| `tutu://special-offers` | Экспериментальные карточки вдохновения, не живые цены. |
| `tutu://version` | Версия, SHA, fingerprint схем. |
| `tutu://debug/memory` | Диагностика. |

Плейбуки доменов — **tools** `get_<domain>_instructions`, не resources.

---

## Что в оффере

Транспорт: `price`, `legs[].segments[]`, `search_results_url`, опционально готовый `checkout_url`, объект `checkout_ref`.

Авиа / автобус / электричка: `variants[]` — семейства тарифов, дешевле первым, у каждого свой `offer_hash`. Багаж, ручная кладь, обмен/возврат, имя тарифа — из `variants[].conditions`. Нет поля в payload → сказать, что Туту не вернул, не додумывать.

ЖД в compact: сводка `fares` (count, price_from/to, refundable/changeable, `seat_categories`). Полная лестница классов — `get_offer_details`.

Отели: `best_offer.checkout_url` + `checkout_ref`. Цена уже за всё пребывание и состав гостей, **не умножать** на ночи. Корзину номера минтит `offerpack_hash` **комнаты** из деталей, не `best_offer.offerpack_hash`.

---

## Checkout: kind

| Продукт | Поведение |
|---|---|
| Авиа | Deeplink на покупку, если в браузере уже есть сессия Туту. Холодный браузер / Telegram WebView часто падает на поиск — пользователь выбирает рейс там. Туда-обратно с пересадкой часто `kind=search_redirect`. |
| ЖД / автобус | Deeplink на выбор мест. После явного выбора мест — `kind=checkout_deeplink` с предвыбранными местами (холодноок). |
| Электричка | URL расписания. |
| Отели | Deeplink на страницу отеля; с room `offerpack_hash` — сразу в корзину. |

`is_multi_pnr` = отдельные билеты / self-transfer: багаж не сквозной, стыковка не защищена. Перед checkout пересказать `multi_pnr_note`.

Число пассажиров прокидывать из `checkout_ref` (`passengers_full/child/infant`), иначе Туту откроет корзину на одного взрослого. Ссылку **не пересобирать и не обрезать** — копировать байт-в-байт.

---

## Правила для агента

- Не выдумывать отели/рейсы, которых нет в текущем `hotels[]` / `offers[]`.
- Не подменять отсутствующие поля веб-поиском или «общими знаниями» про авиакомпанию. Исключение — IATA в авиа-плейбуке.
- Город: смотреть `meta.from` / `meta.to` / `meta.resolved_geo`. Если есть `also_named[]` — назвать выбранный город и альтернативу.
- Отзывы цитировать дословно, с датой, 1–2 фрагмента на плюс/минус.
- Цену рендерить как `amount` + `currency`, без округления.
- У отелей, если не зафиксированы кровать / завтрак / отмена / вид, сначала спросить 2–4 вопроса, потом `search_hotels`. Если пользователь сказал «бери любой / самый дешёвый / кинь ссылку» — не спрашивать, искать сразу.
- Не путать `geo_id` отелей и транспорта: транспортный geo часто без отелей.

---

## Чего в MCP нет

Родной поиск не закрывает все constraints голосом. Это слой продукта.

| Хочет пользователь | В MCP | Что делать агенту |
|---|---|---|
| Именно Хитроу | Да: `destination: "LHR"` / `"Хитроу"` | Сразу аэропорт, не город Лондон |
| Стыковка через Стамбул | Нет параметра `via` | Искать, потом фильтровать `legs[].segments[]` |
| Пересадка 2–5 часов | Нет | Считать по временам сегментов |
| Два чемодана включены | Нет фильтра поиска | Смотреть `variants[].conditions`, отсечь тариф без багажа |
| Питание включено | Нет фильтра поиска | То же по conditions |
| Перевозчик | Да: `carriers` | Имя из `meta.carriers_available` |
| Прямой рейс | Да: `direct_only` | — |
| Вайб с фото | Нет vision | Vision в LLM → город/фильтры отеля → `search_hotels` |

---