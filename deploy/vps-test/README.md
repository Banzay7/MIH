# Тестовый стенд на общем сервере (VPS)

Отдельная тестовая установка MIRTVONLINE на сервере, где уже работают другие сервисы. Стенд ничего не трогает за пределами своих объектов:

| Объект | Имя |
|---|---|
| Контейнер | `mirtvonline-test` |
| Том с базой и снапшотами | `mirtvonline-test-data` |
| Сеть Docker | `mirtvonline-test-net` |
| Образы | `mirtvonline/mih:test-<коммит>` |
| Настройки | `deploy/vps-test/.env` (свой файл, не общий `.env` сервера) |
| Порт | `127.0.0.1:18765` по умолчанию |

Ограничения контейнера: 512 МБ памяти, 1 CPU, 256 процессов, файловая система только для чтения (кроме тома и `/tmp`), без capabilities, логи по 10 МБ × 3.

Всё делается через `./mihctl.sh`. Команда `preflight` только читает и ничего не меняет.

## Что нужно на сервере

- Docker Engine и `docker compose` v2;
- `git`, `curl`, `openssl`;
- около 1 ГБ на диске под образ и базу.

## Установка

```bash
# 1. Код в отдельный каталог (не внутри каталогов других сервисов)
git clone https://github.com/Banzay7/MIH.git ~/mirtvonline-test
cd ~/mirtvonline-test/deploy/vps-test

# 2. Свои настройки стенда
cp .env.example .env && chmod 600 .env
sed -i "s/^MIH_API_TOKEN=.*/MIH_API_TOKEN=$(openssl rand -hex 24)/" .env

# 3. Проверка без изменений: Docker, порт, имена, ресурсы
./mihctl.sh preflight

# 4. Сборка и запуск; в конце печатает live/ready и счётчики каталога
./mihctl.sh deploy
```

Если `preflight` пишет `FAIL`, ничего не запускайте: порт или имя занято, поменяйте `MIH_PUBLISH_PORT` в `.env`.

## Доступ

**Вариант 1, без изменений на сервере (для первого теста).** SSH-туннель с вашего компьютера:

```bash
ssh -N -L 18765:127.0.0.1:18765 <пользователь>@<сервер>
# затем в браузере: http://127.0.0.1:18765/api/docs
```

**Вариант 2, по Tailscale.** Два способа, оба меняют настройки сервера, поэтому только с согласия владельца:

- `tailscale serve --bg --https=18765 http://127.0.0.1:18765` даёт HTTPS внутри tailnet, порт контейнера остаётся на `127.0.0.1`. Отменяется `tailscale serve --https=18765 off`.
- либо в `.env` указать `MIH_PUBLISH_ADDR=<tailscale ip -4>` и выполнить `./mihctl.sh deploy`. Минус: если после перезагрузки Tailscale поднимется позже Docker, контейнер не стартует.

`0.0.0.0` в `MIH_PUBLISH_ADDR` скрипт не пропустит: на VPS это открыло бы API в интернет.

## Проверка после запуска

```bash
T=$(sed -n 's/^MIH_API_TOKEN=//p' .env); H="Authorization: Bearer $T"; B=http://127.0.0.1:18765/api/v1

# плейлист и программа: загрузка файла или источник по URL
SID=$(curl -fsS -H "$H" -H 'content-type: application/json' -d '{"name":"my-playlist"}' $B/sources | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -fsS -H "$H" -F file=@my.m3u "$B/sources/$SID/upload"
EID=$(curl -fsS -H "$H" -H 'content-type: application/json' -d '{"name":"my-epg","url":"https://…/guide.xml.gz"}' $B/epg/sources | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -fsS -H "$H" -X POST "$B/epg/sources/$EID/import"

# поиск и сейчас/далее
curl -fsS -H "$H" -H 'content-type: application/json' -d '{"query":"новости сегодня вечером"}' $B/search
curl -fsS -H "$H" "$B/now"
./mihctl.sh status
```

## Статус и пуши

Планировщик стенда сам обновляет источники по URL, проверяет потоки и утром присылает сводку. Что он делает сейчас и что запланировано, видно на экране «Статус» веб-интерфейса или так:

```bash
docker exec mirtvonline-test mih status
docker exec mirtvonline-test mih run-job digest   # прислать сводку сейчас
docker exec mirtvonline-test mih notify-test      # проверить пуши после настройки MIH_NTFY_URL
```

Настройка ntfy описана в [docs/notifications.md](../../docs/notifications.md). После правки `.env` выполните `./mihctl.sh deploy`, чтобы контейнер перечитал настройки.

## Обслуживание

| Команда | Что делает |
|---|---|
| `./mihctl.sh status` | состояние контейнера, live/ready, счётчики |
| `./mihctl.sh logs 200` | последние строки лога |
| `./mihctl.sh backup` | архив `backups/mih-<время>.tar.gz`: копия SQLite (онлайн, без остановки) и снапшоты |
| `./mihctl.sh restore [файл]` | остановить, восстановить архив (по умолчанию последний), запустить |
| `git pull && ./mihctl.sh deploy` | обновление: сначала автоматический бэкап, потом новый образ |
| `./mihctl.sh rollback` | вернуть предыдущий образ и бэкап, сделанный перед обновлением |
| `./mihctl.sh uninstall --yes` | удалить контейнер, сеть, том и образы стенда; бэкапы остаются |

Откат возвращает и образ, и базу: новая версия могла обновить схему, а старая её не поймёт. Данные, добавленные после обновления, при откате теряются; если они нужны, сделайте `backup` перед `rollback`.

Бэкапы лежат рядом со скриптом в `backups/`. Чтобы держать копии вне сервера, забирайте их `scp` или добавьте `./mihctl.sh backup` в cron пользователя.

## Чего стенд не делает

- не читает и не меняет чужие контейнеры, тома, сети, `.env` и службы;
- не открывает порты наружу и не меняет firewall;
- не ставит пакеты на хост.
