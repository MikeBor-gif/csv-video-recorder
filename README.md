# CSV-driven Browser Automation Video Recorder

> Записывает видео автоматизированных действий в браузере — по одному `.webm` на каждую строку CSV — и при желании загружает их на веб-сервис с просмотром в браузере.

Python + Playwright (sync API, Chromium). Для каждой строки `input.csv` скрипт открывает DuckDuckGo, плавно ведёт видимый SVG-курсор к строке поиска, посимвольно набирает `Magento developer in <City>`, запускает поиск и кликает первый результат. Всё это записывается на видео с визуальными эффектами.

Веб-сервис на Flask (`web_service.py`) принимает видео, хранит файлы в `uploads/`, а название компании, путь и URL — в SQLite (`videos.db`). С опцией `--server` рекордер загружает каждое видео и открывает его в Chromium.

## Быстрый старт

Требуется Python 3.9+ и доступ в интернет. Сторонние зависимости — только `playwright`, `flask` и `requests`.

```bash
git clone https://github.com/MikeBor-gif/csv-video-recorder.git
cd csv-video-recorder

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
python web_service.py                                          # терминал 1: сервис на 0.0.0.0:8000
python capture_video.py --server http://127.0.0.1:8000         # терминал 2: запись → загрузка → просмотр
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
- **Веб-сервис** — Flask + SQLite: загрузка видео, поиск URL по названию компании, раздача файлов для просмотра в браузере.
- **Загрузка и просмотр** — с `--server` каждое записанное видео загружается на сервис и открывается в Chromium.

## Пример

```text
$ python capture_video.py
[INFO] [Acme Magento Minsk] start: url=https://duckduckgo.com/ city=Minsk language=ru country=Belarus
[INFO] [Acme Magento Minsk] opened https://duckduckgo.com/
...
[INFO] [Acme Magento Minsk] saved: E:\job_task\video\Acme Magento Minsk.webm
E:\job_task\video\Acme Magento Minsk.webm
...
Done: 2/2 videos saved
```

С `--server` после пути к видео добавляются строки `[INFO] [<company>] uploaded: http://127.0.0.1:8000/media/<file>.webm` и `preview opened`, а итог — `Done: 2/2 videos saved, 2 uploaded`.

Видео (1280×720) сохраняются как `./video/<Company Name>.webm`. Недопустимые для имени файла символы заменяются на `_`, а существующий файл с тем же именем перезаписывается.

## Формат входного CSV

UTF-8 (BOM, который добавляет Excel, допускается), разделитель `;`, кавычки `"`. Заголовок обязателен и должен совпадать точно:

```text
URL;Company Name;Language;Country;Postcode;City;Street;HouseNo
"https://duckduckgo.com/";"Acme Magento Minsk";"ru";"Belarus";"220030";"Minsk";"Nezavisimosti Ave";"4"
```

В сэмпл-сценарии используются `URL`, `City` (в поисковом запросе) и `Company Name` (имя видео). Остальные колонки передаются в функцию и пишутся в лог.

## Веб-сервис

`python web_service.py` при первом запуске создаёт рядом со скриптом `videos.db` и `uploads/`.

| Метод | Путь | Назначение |
|-------|------|------------|
| `POST` | `/upload` | UploadVideo: multipart `file` + `company_name` → `200 {"company_name", "video_url"}` |
| `GET` | `/get_video_url?company_name=<name>` | GetVideoURL: `200 {"company_name", "video_url"}` или `404` |
| `GET` | `/media/<filename>` | Отдаёт загруженное видео (поддерживает Range-запросы) |
| `GET` | `/videos` | Список всех записей |

```bash
curl -F "file=@video/Acme Magento Minsk.webm" -F "company_name=ACME GmbH" http://127.0.0.1:8000/upload
# {"company_name": "ACME GmbH", "video_url": "http://127.0.0.1:8000/media/ACME_GmbH.webm"}

curl "http://127.0.0.1:8000/get_video_url?company_name=ACME%20GmbH"
# {"company_name": "ACME GmbH", "video_url": "http://127.0.0.1:8000/media/ACME_GmbH.webm"}
```

- **Повторная загрузка** того же `company_name` обновляет запись (upsert) и заменяет файл — дублей нет.
- **Имя файла** строится из названия компании (`ACME GmbH` → `ACME_GmbH.webm`). Если название не содержит латиницы или совпадает по имени файла с другой компанией, добавляется короткий хеш (`video_1a2b3c4d.webm`).
- **URL** строится из адреса запроса (`request.host_url`), поэтому при загрузке через `127.0.0.1` ссылка тоже будет на `127.0.0.1`.
- **Ошибки** возвращаются как JSON `{"error": ...}`: `400` — нет файла / пустое название / неподдерживаемый тип (разрешены `.webm`, `.mp4`, `.ogg`, `.mov`), `404` — компания или файл не найдены, `413` — файл больше лимита.

Схема базы (видео в БД не хранится — только путь и URL):

```sql
CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT    NOT NULL UNIQUE,
    file_path    TEXT    NOT NULL,  -- абсолютный путь к файлу
    video_url    TEXT    NOT NULL,  -- публичный URL видео
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
```

Каждый запрос открывает своё соединение SQLite; включены WAL и busy timeout, поэтому параллельные загрузки не падают с `database is locked`.

## Настройки

| Что | Где | По умолчанию | Описание |
|-----|-----|--------------|----------|
| `PATH_TO_VIDEO_FOLDER` | `qscript.py` (переопределяется в `capture_video.py`) | `./video` | Папка для видео; создаётся автоматически |
| `HEADLESS` | `qscript.py` | `False` | Запуск браузера без окна. DuckDuckGo надёжнее работает с видимым окном |
| `VIDEO_SIZE` | `qscript.py` | 1280×720 | Viewport и размер видео |
| `RESULTS_TIMEOUT_MS` | `qscript.py` | `30000` | Ожидание результатов поиска |
| `QSCRIPT_LOG_LEVEL` | переменная окружения | `INFO` | `DEBUG` / `INFO` / `WARN` / `ERROR` для рекордера и `video_client` |
| `FLASK_HOST` / `FLASK_PORT` | переменные окружения | `0.0.0.0` / `8000` | Адрес и порт `web_service.py` |
| `WEB_SERVICE_LOG_LEVEL` | переменная окружения | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` для сервиса |
| `MAX_UPLOAD_MB` | переменная окружения | `512` | Максимальный размер загружаемого файла |

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

url = video_client.UploadVideo("http://127.0.0.1:8000", "video/Acme.webm", "Acme")
same = video_client.GetVideoURL("http://127.0.0.1:8000", "Acme")
video_client.PreviewVideo(url, view_seconds=5)
```

- `UploadVideo(server_url: str, video_path: str, company_name: str) -> str` — загружает видео и возвращает его URL.
- `GetVideoURL(server_url: str, company_name: str) -> str` — возвращает URL видео компании.
- `PreviewVideo(video_url, view_seconds=10, company_name="preview")` — открывает URL в Chromium (`headless=False`); `0` — до нажатия Enter; закрытие окна пользователем завершает просмотр раньше. `company_name` используется только в логах.
- Любой сбой (сеть, HTTP-ошибка, неизвестная компания, нет файла) — исключение `VideoClientError`. `requests` и Playwright импортируются внутри функций.

## Тесты

```bash
python -m unittest discover -s tests -v
```

Тесты используют временную папку и не трогают `videos.db` / `uploads/` проекта: `tests/test_web_service.py` проверяет эндпоинты через Flask test client, `tests/test_video_client.py` — клиент против сервера, поднятого в фоновом потоке. `PreviewVideo` (видимый браузер) проверяется вручную.

## Структура

| Файл | Назначение |
|------|------------|
| `capture_video.py` | Точка входа: читает CSV через `csv.DictReader(f, delimiter=";", quotechar='"')` и вызывает сценарий для каждой строки |
| `qscript.py` | `PATH_TO_VIDEO_FOLDER`, `CaptureVideo_Magento`, `ShowCallout`, JS-оверлеи курсора |
| `web_service.py` | Flask-сервис и слой SQLite: `/upload`, `/get_video_url`, `/media/<file>`, `/videos` |
| `video_client.py` | `UploadVideo`, `GetVideoURL`, `PreviewVideo` — клиент для рекордера |
| `tests/` | `unittest`-тесты сервиса и клиента |
| `requirements.txt` | `playwright>=1.51`, `flask>=2.2`, `requests>=2.28` (с `--prefer-binary` для установки на Python 3.9 без компилятора) |
| `input.csv` | Пример входных данных |
| `uploads/`, `videos.db` | Данные сервиса (создаются при запуске, в git не попадают) |
