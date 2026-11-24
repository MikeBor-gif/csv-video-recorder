# Browser Video Recorder

Запись браузерного сценария с зумом, хранение видео и извлечение реквизитов компаний из Impressum.

Task 4: `python magento_cart_agent.py --urls urls.txt` — Browser-Use + DeepSeek исследуют магазин, проверяют Magento-корзину и сохраняют screenshot формы доставки. Нужен Python 3.11+; [установка и проверки](docs/cart-agent.md).

Для проверки Task 3 начните с [запуска с нового компьютера](docs/companies.md#запуск-с-нового-компьютера). Эту часть можно запустить и проверить целиком через Docker, без локальных Python и Chromium.

Рекордер на Python и Playwright открывает DuckDuckGo, вводит `Magento developer in <City>` и переходит к первому результату. Для каждой компании создаётся WebM 1280×720 с курсором, подсветкой кликов и текстовой плашкой.

F5 или `window.__zoomAt(x, y)` переключают приближение и отдаление. События сохраняются рядом с видео; ffmpeg применяет зум после загрузки. Flask работает под Apache + mod_wsgi, PostgreSQL 16 хранит метаданные и состояния обработки.

## Запуск

Нужны Docker с Compose v2 и доступ в интернет для сборки и внешних запросов. Python 3.9+ нужен для рекордера и локальных Python-команд; Chromium устанавливается для записи видео. Локальный ffmpeg нужен только для обработки вне Docker.

### Веб-сервис

```bash
docker compose up --build -d --wait
```

Адрес: http://localhost:8000. Настройки находятся в [.env.example](.env.example): при необходимости скопируйте его в `.env` и измените значения. Данные сохраняются в томах `db_data` и `uploads` после остановки контейнеров.

### Рекордер

```bash
python -m venv .venv
```

Активируйте окружение:

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

```bash
# Linux / macOS
source .venv/bin/activate
```

На Debian/Ubuntu перед установкой зависимостей нужны `apache2-dev` и `gcc` для сборки mod_wsgi. На Windows/macOS этот пакет пропускается.

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python capture_video.py input.csv --zoom-demo --server http://localhost:8000
```

Откроется Chromium. Для каждой строки CSV программа запишет видео, загрузит его, дождётся обработки и откроет готовый ролик. Без `--zoom-demo` зум переключается вручную клавишей F5; без `--server` файлы остаются локально в `video/`.

| Опция | Назначение |
|---|---|
| `csv_path` | Входной CSV, по умолчанию `input.csv` |
| `--zoom-demo` | Автоматически включить и выключить зум во время ввода запроса |
| `--server URL` | Загрузить запись и открыть результат |
| `--view-seconds N` | Длительность просмотра, по умолчанию 10 секунд; `0` — до Enter |

Сценарий зависит от доступности DuckDuckGo. Ошибка одной строки не прерывает остальные. Если настроен HTTP-прокси, добавьте `localhost,127.0.0.1` в `NO_PROXY`.

## CSV

UTF-8, разделитель `;`, кавычки `"`; BOM допускается. Заголовки колонок обязательны:

```csv
URL;Company Name;Language;Country;Postcode;City;Street;HouseNo
"https://duckduckgo.com/";"Acme Magento Minsk";"ru";"Belarus";"220030";"Minsk";"Nezavisimosti Ave";"4"
```

Готовый файл: `video/<Company Name>.webm`. Недопустимые символы имени заменяются; повторная запись той же компании заменяет прежнее видео.

## Компании и Impressum (Task 3)

Все команды выполняйте в папке клонированного репозитория. При первом запуске создайте локальный `.env` из шаблона; существующий `.env` не перезаписывайте:

```bash
cp .env.example .env
```

Откройте `.env` в текстовом редакторе, замените `your_deepseek_api_key` своим ключом и сохраните:

```dotenv
DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_MODEL=deepseek-flash
```

Запустите сервис:

```bash
docker compose up -d --build --wait
```

Откройте http://localhost:8000/upload_companies и загрузите [companies.csv](companies.csv). Затем выполните извлечение (использует платный DeepSeek API):

```bash
docker compose exec -T web python enrich_companies.py --limit 2
```

Результат: http://localhost:8000/companies. `status: 0` означает полный адрес. Уже OK-компании пропускаются; повторный импорт сохраняет извлечённые данные. Максимум две API-попытки на компанию. [Проверки Task 3 без платных запросов и локального Python](docs/companies.md#проверка-и-наблюдаемый-результат).

## Проверка

Сквозная проверка зума создаёт тестовое видео, кодирует его в контейнере и проверяет URL, PostgreSQL, разрешение, длительность и копирование без событий:

```bash
python scripts/verify_zoom.py --restart
```

Ожидаемый результат: `ALL ZOOM CHECKS PASSED`. Флаг `--restart` дополнительно проверяет сохранность после перезапуска. Проверка прежнего API и Docker-стека:

```bash
python scripts/verify_docker.py
```

Тесты сервиса выполняются с отдельной базой `<DB_NAME>_test`:

```bash
docker compose exec -T web python -m pytest tests -m "not browser" -v
```

Браузерные тесты запускаются локально с Chromium; для проверки кодирования нужны ffmpeg и ffprobe:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_zoom_browser.py -v
```

Пропущенные тесты означают, что соответствующая проверка не выполнена. Сквозные проверки оставляют тестовые записи в локальном сервисе; `verify_docker.py` перезапускает стек.

## Документация

| Руководство | Содержание |
|---|---|
| [Зум и API](docs/zoom.md) | Sidecar, ffmpeg CLI, загрузка, jobs API и настройки |
| [Компании и Impressum](docs/companies.md) | Импорт CSV, companies API, DeepSeek CLI, бюджет и проверки |
| [Подробное объяснение лабораторной](docs/LAB_EXPLANATION.md) | Методички, теория, разбор кода, запуск и подготовка к защите |
| [Агент корзины — Task 4](docs/cart-agent.md) | CDP, DeepSeek, checkout screenshots, настройки и проверки |

Логи: `docker compose logs -f web`. Остановка с сохранением данных: `docker compose down`.
