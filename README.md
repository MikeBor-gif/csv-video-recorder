# CSV-driven Browser Automation Video Recorder

> Записывает видео автоматизированных действий в браузере — по одному `.webm` на каждую строку CSV — и загружает их на веб-сервис для просмотра в браузере.

Python + Playwright (sync API, Chromium). Для каждой строки `input.csv` скрипт открывает DuckDuckGo, плавно ведёт видимый SVG-курсор к строке поиска, посимвольно набирает `Magento developer in <City>`, запускает поиск и кликает первый результат. Всё это записывается на видео с визуальными эффектами.

Веб-сервис на Flask (`web_service.py`) принимает видео, хранит файлы в `uploads/`, а название компании, путь и URL — в **PostgreSQL**. Сервис работает в Docker: **Apache + mod_wsgi** в контейнере `web`, **PostgreSQL 16** — в отдельном контейнере `db`; оба запускаются через **Docker Compose**. С опцией `--server` рекордер загружает каждое видео и открывает его в Chromium.

## Docker: веб-сервис и база данных (Internship Task 1)

```text
  host :8000
      │
      ▼ :80
┌────────────────────────────────┐        ┌───────────────────────┐
│ web (Dockerfile)               │  5432  │ db (postgres:16)      │
│ Apache 2.4 + mod_wsgi          ├───────►│ таблица videos        │
│  /        → web_service.wsgi   │        │                       │
│  /media/  → Alias /app/uploads │        │                       │
└───────────────┬────────────────┘        └───────────┬───────────┘
                │ том uploads                         │ том db_data
                ▼ /app/uploads                        ▼ /var/lib/postgresql/data
```

- `web` стартует только после того, как healthcheck `db` (`pg_isready`) стал успешным (`depends_on: condition: service_healthy`). Кроме того, приложение при старте само повторяет подключение к БД (до `DB_CONNECT_RETRIES` попыток), поэтому не падает, если база ещё не готова.
- mod_wsgi предзагружает приложение при старте Apache: подключение к БД и создание таблицы происходят до первого запроса.
- Настройки БД передаются только через переменные окружения `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`.
- Загруженные видео отдаёт сам Apache (`Alias /media/`), с поддержкой Range-запросов.
- Данные БД и видео лежат в именованных томах `db_data` и `uploads` и сохраняются после `docker compose down` / `up`.

### Запуск

Требуется Docker с Compose v2 (Docker Desktop на Windows/macOS или Docker Engine на Linux).

```bash
git clone https://github.com/MikeBor-gif/csv-video-recorder.git
cd csv-video-recorder

docker compose up --build
```

Сервис доступен на `http://localhost:8000`. Для запуска в фоне: `docker compose up --build -d`.

Пароль БД и другие настройки можно задать в `.env` (файл в git не попадает). Без `.env` используются значения по умолчанию для локального запуска:

```bash
cp .env.example .env      # затем поменяйте DB_PASSWORD
```

### Автоматическая проверка

Скрипт проходит весь чек-лист приёмки и печатает `[PASS]` / `[FAIL]` по каждому пункту. Нужен Python 3.9+, сторонние пакеты не нужны:

```bash
python scripts/verify_docker.py
```

| Пункт чек-листа | Что проверяет скрипт |
|---|---|
| `docker compose up --build` поднимает оба контейнера | `up --build -d --wait`; `web` и `db` в состоянии running/healthy; `GET /health` → `{"status": "ok"}` |
| `POST /upload` загружает видео и возвращает рабочий URL | загрузка файла, скачивание по полученному URL, байты совпадают, `Content-Type: video/webm` |
| `GET /get_video_url?company_name=...` возвращает URL | URL совпадает с URL загрузки; неизвестная компания → `404` |
| После `docker compose down` и `up` данные и видео сохранены | `down` (без `-v`) → `up` → запись и файл на месте |
| `web` ждёт готовности `db` и не падает | у `web` нет перезапусков, в логе есть `database ready`, нет `Traceback` |
| unit-тесты | `python -m unittest` внутри контейнера `web` |

В конце выводится `ALL CHECKS PASSED` (код выхода `0`). Опции: `--skip-build`, `--skip-tests`, `--down-after` (остановить контейнеры, тома сохраняются), `--base-url`.

### Ручная проверка

```bash
curl http://localhost:8000/health
# {"status":"ok"}

curl -F "file=@video/Acme Magento Minsk.webm" -F "company_name=ACME GmbH" http://localhost:8000/upload
# {"company_name":"ACME GmbH","video_url":"http://localhost:8000/media/ACME_GmbH.webm"}

curl "http://localhost:8000/get_video_url?company_name=ACME%20GmbH"
# {"company_name":"ACME GmbH","video_url":"http://localhost:8000/media/ACME_GmbH.webm"}

docker compose down && docker compose up -d     # данные и видео сохраняются
curl "http://localhost:8000/get_video_url?company_name=ACME%20GmbH"

docker compose exec web curl http://localhost/  # запрос изнутри контейнера
docker compose exec db psql -U video_user -d videos -c "SELECT * FROM videos;"
```

Любой `.webm` подойдёт для загрузки. Видео из рекордера можно загрузить сразу: `python capture_video.py --server http://localhost:8000` (см. ниже).

### Логи, тесты, остановка

```bash
docker compose logs -f web            # Apache access/error log и логи приложения
docker compose exec -T web python -m unittest discover -s tests -v
docker compose down                   # остановить; данные сохраняются
docker compose down -v                # остановить и УДАЛИТЬ тома с БД и видео
```

Уровень логов приложения — `WEB_SERVICE_LOG_LEVEL` в `.env` (`DEBUG` / `INFO` / `WARNING` / `ERROR`).

## Рекордер: быстрый старт

Требуется Python 3.9+ и доступ в интернет.

```bash
python -m venv .venv
.venv\Scripts\activate              # Windows (PowerShell / cmd)
# source .venv/bin/activate         # Linux / macOS

pip install -r requirements.txt
python -m playwright install chromium

python capture_video.py             # или: python capture_video.py path/to/other.csv
```

Откроется окно Chromium, сценарий выполнится для каждой строки `input.csv` (~17 c на строку). Готовые видео — в папке `video/`.

### Запись + загрузка + просмотр

```bash
docker compose up --build -d                                   # веб-сервис в Docker
python capture_video.py --server http://localhost:8000         # запись → загрузка → просмотр
```

После каждого успешно записанного видео рекордер вызывает `UploadVideo`, печатает полученный URL и открывает его в Chromium (встроенный видеоплеер).

| Опция | По умолчанию | Описание |
|-------|--------------|----------|
| `csv_path` | `input.csv` | Входной CSV (позиционный аргумент) |
| `--server URL` | — | Адрес веб-сервиса; без опции рекордер работает как раньше и не требует `requests` |
| `--view-seconds N` | `10` | `> 0` — держать браузер N секунд; `0` — до нажатия Enter |

Ошибка загрузки или просмотра пишется в лог (`[ERROR] [<company>] upload failed: ...`), обработка остальных строк продолжается.

## Возможности

- **SVG-курсор** — белая стрелка с чёрной обводкой (32×32) поверх страницы; Playwright не записывает системный курсор.
- **Жёлтая подсветка** — полупрозрачный круг (r ≈ 32 px) под стрелкой, следует за курсором.
- **Click ripple** — волна от точки каждого клика.
- **Callout** — жёлтая плашка с текстом 16pt и плавным появлением/исчезанием (~2 c).
- **Плавное движение и «человеческий» ввод** — без телепортов курсора, случайные задержки между символами.
- **Устойчивость** — ошибка в одной строке CSV не останавливает остальные.
- **Веб-сервис** — Flask + PostgreSQL за Apache/mod_wsgi в Docker: загрузка видео, поиск URL по названию компании, раздача файлов для просмотра в браузере.
- **Загрузка и просмотр** — с `--server` каждое записанное видео загружается на сервис и открывается в Chromium.

## Пример

```text
$ python capture_video.py --server http://localhost:8000
[INFO] [Acme Magento Minsk] start: url=https://duckduckgo.com/ city=Minsk language=ru country=Belarus
[INFO] [Acme Magento Minsk] opened https://duckduckgo.com/
...
[INFO] [Acme Magento Minsk] saved: E:\job_task\video\Acme Magento Minsk.webm
E:\job_task\video\Acme Magento Minsk.webm
[INFO] [Acme Magento Minsk] uploaded: http://localhost:8000/media/Acme_Magento_Minsk.webm
[INFO] [Acme Magento Minsk] preview opened: http://localhost:8000/media/Acme_Magento_Minsk.webm (10s)
...
Done: 2/2 videos saved, 2 uploaded
```

Видео (1280×720) сохраняются как `./video/<Company Name>.webm`. Недопустимые для имени файла символы заменяются на `_`, а существующий файл с тем же именем перезаписывается.

## Формат входного CSV

UTF-8 (BOM, который добавляет Excel, допускается), разделитель `;`, кавычки `"`. Заголовок обязателен и должен совпадать точно:

```text
URL;Company Name;Language;Country;Postcode;City;Street;HouseNo
"https://duckduckgo.com/";"Acme Magento Minsk";"ru";"Belarus";"220030";"Minsk";"Nezavisimosti Ave";"4"
```

В сэмпл-сценарии используются `URL`, `City` (в поисковом запросе) и `Company Name` (имя видео). Остальные колонки передаются в функцию и пишутся в лог.

## Веб-сервис

| Метод | Путь | Назначение |
|-------|------|------------|
| `POST` | `/upload` | UploadVideo: multipart `file` + `company_name` → `200 {"company_name", "video_url"}` |
| `GET` | `/get_video_url?company_name=<name>` | GetVideoURL: `200 {"company_name", "video_url"}` или `404` |
| `GET` | `/media/<filename>` | Отдаёт загруженное видео (поддерживает Range-запросы) |
| `GET` | `/videos` | Список всех записей |
| `GET` | `/health` | Проверка БД (`SELECT 1`): `200 {"status": "ok"}` или `503` — используется healthcheck контейнера |
| `GET` | `/` | Краткое описание сервиса и список эндпоинтов |

- **Повторная загрузка** того же `company_name` обновляет запись (upsert) и заменяет файл — дублей нет.
- **Имя файла** строится из названия компании (`ACME GmbH` → `ACME_GmbH.webm`). Если название не содержит латиницы или совпадает по имени файла с другой компанией, добавляется короткий хеш (`video_1a2b3c4d.webm`).
- **URL** строится из адреса запроса (`request.host_url`): при обращении к `http://localhost:8000` ссылка тоже будет на `localhost:8000`.
- **Ошибки** возвращаются как JSON `{"error": ...}`: `400` — нет файла / пустое название / неподдерживаемый тип (разрешены `.webm`, `.mp4`, `.ogg`, `.mov`), `404` — компания или файл не найдены, `413` — файл больше лимита.

Схема базы (видео в БД не хранится — только путь и URL); таблица создаётся при старте сервиса:

```sql
CREATE TABLE IF NOT EXISTS videos (
    id           SERIAL      PRIMARY KEY,
    company_name TEXT        NOT NULL UNIQUE,
    file_path    TEXT        NOT NULL,  -- абсолютный путь к файлу
    video_url    TEXT        NOT NULL,  -- публичный URL видео
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Каждый запрос открывает своё соединение `psycopg2` (соединения psycopg2 не потокобезопасны); upsert — `INSERT ... ON CONFLICT (company_name) DO UPDATE`.

Без Docker сервис можно запустить dev-сервером Flask (`python web_service.py`, порт `8000`), если задать `DB_*` для доступной PostgreSQL.

## Настройки

| Что | Где | По умолчанию | Описание |
|-----|-----|--------------|----------|
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | `.env` → `docker-compose.yml` | `videos` / `video_user` / `video_pass` | База, пользователь и пароль PostgreSQL (одни и те же для `web` и `db`) |
| `DB_HOST` / `DB_PORT` | `docker-compose.yml` | `db` / `5432` | Адрес PostgreSQL для веб-сервиса (обязательны при запуске без Docker) |
| `DB_CONNECT_RETRIES` / `DB_CONNECT_DELAY` | переменные окружения | `30` / `2` c | Сколько раз и с каким интервалом сервис пытается подключиться к БД при старте |
| `WEB_SERVICE_LOG_LEVEL` | `.env` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` для сервиса |
| `MAX_UPLOAD_MB` | `.env` | `512` | Максимальный размер загружаемого файла |
| `UPLOAD_DIR` | `Dockerfile` | `/app/uploads` | Папка для видео (том `uploads`) |
| `FLASK_HOST` / `FLASK_PORT` | переменные окружения | `0.0.0.0` / `8000` | Адрес и порт dev-сервера `python web_service.py` |
| `PATH_TO_VIDEO_FOLDER` | `qscript.py` (переопределяется в `capture_video.py`) | `./video` | Папка для видео рекордера; создаётся автоматически |
| `HEADLESS` | `qscript.py` | `False` | Запуск браузера без окна. DuckDuckGo надёжнее работает с видимым окном |
| `VIDEO_SIZE` | `qscript.py` | 1280×720 | Viewport и размер видео |
| `RESULTS_TIMEOUT_MS` | `qscript.py` | `30000` | Ожидание результатов поиска |
| `QSCRIPT_LOG_LEVEL` | переменная окружения | `INFO` | `DEBUG` / `INFO` / `WARN` / `ERROR` для рекордера и `video_client` |

```bash
QSCRIPT_LOG_LEVEL=DEBUG python capture_video.py      # Linux/macOS, Git Bash
$env:QSCRIPT_LOG_LEVEL="DEBUG"; python capture_video.py   # PowerShell
```

## API `qscript`

```python
import qscript

qscript.PATH_TO_VIDEO_FOLDER = "./out"          # необязательно
path = qscript.CaptureVideo_Magento(
    url="https://duckduckgo.com/", company_name="Acme", language="en",
    country="US", postcode="10001", city="New York", street="5th Ave", house_no="1",
)
# -> абсолютный путь к .webm или None при ошибке
```

- `CaptureVideo_Magento(url, company_name, language, country, postcode, city, street, house_no)` — запускает сценарий и возвращает абсолютный путь к видео или `None` при ошибке. Исключения наружу не пробрасываются.
- `ShowCallout(text, bgColor="#FFFF00", textColor="#000000", fade=True, page=None)` — показывает плашку с текстом и сразу возвращает управление: плашка проигрывается, пока сценарий продолжает действия. Без `page` использует страницу текущего сценария, поэтому вызывается изнутри `CaptureVideo_Magento`.

## API `video_client`

```python
import video_client

url = video_client.UploadVideo("http://localhost:8000", "video/Acme.webm", "Acme")
same = video_client.GetVideoURL("http://localhost:8000", "Acme")
video_client.PreviewVideo(url, view_seconds=5)
```

- `UploadVideo(server_url: str, video_path: str, company_name: str) -> str` — загружает видео и возвращает его URL.
- `GetVideoURL(server_url: str, company_name: str) -> str` — возвращает URL видео компании.
- `PreviewVideo(video_url, view_seconds=10, company_name="preview")` — открывает URL в Chromium (`headless=False`); `0` — до нажатия Enter; закрытие окна пользователем завершает просмотр раньше. `company_name` используется только в логах.
- Любой сбой (сеть, HTTP-ошибка, неизвестная компания, нет файла) — исключение `VideoClientError`. `requests` и Playwright импортируются внутри функций.

## Тесты

Тестам нужна PostgreSQL, поэтому они запускаются внутри контейнера `web`:

```bash
docker compose exec -T web python -m unittest discover -s tests -v
```

Тесты работают в отдельной базе `<DB_NAME>_test` (создаётся автоматически) и во временной папке, поэтому не трогают рабочую таблицу `videos` и том `uploads`. `tests/test_web_service.py` проверяет эндпоинты через Flask test client, `tests/test_video_client.py` — клиент против сервера, поднятого в фоновом потоке. Без PostgreSQL (например, `python -m unittest` на хосте) тесты БД пропускаются с подсказкой. `PreviewVideo` (видимый браузер) проверяется вручную.

## Структура

| Файл | Назначение |
|------|------------|
| `docker-compose.yml` | Сервисы `web` (Apache + Flask, порт `8000:80`) и `db` (`postgres:16`), тома `db_data` и `uploads`, healthcheck |
| `Dockerfile` | Образ `web`: Python 3.12 + Apache 2.4 + mod_wsgi (собирается pip-пакетом `mod-wsgi` под Python образа) |
| `apache/video-service.conf` | Apache VirtualHost: `WSGIDaemonProcess`, `WSGIScriptAlias`, `Alias /media/` |
| `web_service.wsgi` | WSGI-точка входа: `from web_service import app as application` |
| `web_service.py` | Flask-сервис и слой PostgreSQL: `/upload`, `/get_video_url`, `/media/<file>`, `/videos`, `/health` |
| `.env.example` | Пример настроек для `.env` (пароль БД и др.) |
| `scripts/verify_docker.py` | Сквозная проверка чек-листа приёмки |
| `capture_video.py` | Точка входа рекордера: читает CSV через `csv.DictReader(f, delimiter=";", quotechar='"')` и вызывает сценарий для каждой строки |
| `qscript.py` | `PATH_TO_VIDEO_FOLDER`, `CaptureVideo_Magento`, `ShowCallout`, JS-оверлеи курсора |
| `video_client.py` | `UploadVideo`, `GetVideoURL`, `PreviewVideo` — клиент для рекордера |
| `tests/` | `unittest`-тесты сервиса и клиента (`pg_helpers.py` — тестовая база PostgreSQL) |
| `requirements.txt` | `playwright`, `flask`, `requests`, `psycopg2-binary`, `mod-wsgi` (только Linux — ставится в Docker-образе) |
| `input.csv` | Пример входных данных |
