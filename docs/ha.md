# Home Assistant: «На ТВ» напрямую

Кнопка «На ТВ» может идти прямо в Home Assistant, без агента: сервер MIRTVONLINE вызывает ваш скрипт HA через REST API и передаёт ему адреса потоков и название канала. Как включить канал на телевизоре, решает сам скрипт. MIRTVONLINE ничего не меняет в настройках Home Assistant.

## Что нужно

1. **Связь сервера с Home Assistant.** Сервер должен открывать адрес HA, например `http://homeassistant.local:8123` в той же сети, адрес HA в Tailscale (`http://100.x.y.z:8123`) или внешний адрес Nabu Casa. Проверка с сервера: `curl -s -o /dev/null -w '%{http_code}\n' <адрес>/api/` отвечает `401` (адрес доступен, токена ещё нет).
2. **Долгоживущий токен HA.** В Home Assistant: нажмите своё имя внизу слева → вкладка **Security** (Безопасность) → **Long-lived access tokens** (Долгосрочные токены доступа) → **Create token** (Создать токен) → введите имя, например `mirtvonline` → **OK** → скопируйте токен (он показывается один раз). Токен вписывается только в `.env` на сервере, его не нужно пересылать в чат или класть в git.
3. **Скрипт в HA**, который включает канал. Свой готовый скрипт подходит: укажите его имя в `MIH_HA_SCRIPT`.

## Настройки

| Переменная | По умолчанию | Что это |
|---|---|---|
| `MIH_HA_URL` | нет | Адрес Home Assistant, без `/` в конце |
| `MIH_HA_TOKEN` | нет | Долгоживущий токен HA |
| `MIH_HA_SCRIPT` | `mih_play` | Скрипт, который запускается (`script.` в начале можно не писать) |
| `MIH_HA_STREAMS` | `3` | Сколько адресов потоков передавать в `urls` |
| `MIH_HA_FIELDS` | нет | Переименование переменных под ваш скрипт: `channel=name,url=stream_url`; `stream_id=` (пусто) не передаёт переменную |
| `MIH_HA_TIMEOUT_S` | `60` | Сколько ждать, пока скрипт отработает |
| `MIH_PLAYBACK` | `agent` | `ha` делает HA способом по умолчанию для всех браузеров |

Без `MIH_HA_URL` и `MIH_HA_TOKEN` мост выключен, и веб-интерфейс его не предлагает.

## Переменные, которые получает скрипт

| Переменная | Значение |
|---|---|
| `url` | Лучший поток канала (тот, что работал последним; потоки, которым нужны особые заголовки, идут в конце) |
| `urls` | До `MIH_HA_STREAMS` потоков, лучший первым: запасные, если первый не заиграет |
| `channel` | Название канала |
| `channel_id` | ID канала в MIRTVONLINE |
| `stream_id`, `command_id` | ID потока и команды, для журнала |
| `play_url` | Подписанная ссылка `MIH_PUBLIC_URL/play/<канал>`, которая сама выберет живой поток; только если задан `MIH_PUBLIC_URL` |

Пример скрипта для медиаплеера, который умеет открывать адрес (`scripts.yaml`):

```yaml
mih_play:
  alias: MIRTVONLINE play
  fields:
    url: {description: Stream URL}
    channel: {description: Channel name}
  sequence:
    - action: media_player.play_media
      target:
        entity_id: media_player.living_room_tv
      data:
        media_content_id: "{{ url }}"
        media_content_type: url
```

Сервер вызывает скрипт напрямую (`POST /api/services/script/<имя>?return_response`) и ждёт, пока он закончится: скрипт отработал — команда `started`, интерфейс пишет «Home Assistant выполнил скрипт». Скрипт может сам сообщить результат: последний шаг `stop: … response_variable: result` с `result: {ok: false, error: "…"}` даёт ошибку `ha_not_playing`, и интерфейс показывает текст `error`. Ошибки: `ha_unreachable` (нет связи), `ha_unauthorized` (токен не принят), `ha_script_not_found` (нет такого скрипта), `ha_script_failed` (скрипт упал, подробности в журнале HA), `ha_timeout` (не ответил за `MIH_HA_TIMEOUT_S`; канал, возможно, всё же включится). Адреса потоков уходят только в HA и в ответ браузеру не попадают.

## В веб-интерфейсе

**Настройки** → блок **Кнопка «На ТВ»**: выберите «Напрямую через Home Assistant». Выбор хранится в браузере, так что телефон может идти через HA, а другой браузер через агента. Кнопка **Проверить связь с HA** проверяет адрес, токен и наличие скрипта, ничего не запуская.

API: `POST /api/v1/playback/commands` с `"adapter": "ha"` или `"agent"`; `GET /api/v1/playback/adapters` — что доступно; `POST /api/v1/playback/ha/check` — проверка связи.

## Передача с начала (catch-up)

В `POST /api/v1/playback/commands` в `target` можно передать `airing_id` (эфир из программы). Если передача уже идёт или закончилась, сервер строит ссылку архива по атрибутам плейлиста (`catchup`, `catchup-source`, `catchup-days`; режимы `default`, `append`, `shift`, `flussonic`) и передаёт скрипту `catchup_url`, `start_ts` (UTC ISO) и `program`. Скрипт HA играет `catchup_url`, если он есть, иначе обычный `url`. Если у источника нет правила архива, канал играет вживую, а команда получает `detail: live_no_catchup`. Передача, которая ещё не началась, запускается как обычный канал.
