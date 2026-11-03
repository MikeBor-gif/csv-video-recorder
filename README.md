# CSV-driven Browser Automation Video Recorder

> Записывает видео автоматизированных действий в браузере — по одному `.webm` на каждую строку CSV.

Python + Playwright (sync API, Chromium). Для каждой строки `input.csv` скрипт открывает DuckDuckGo, плавно ведёт видимый SVG-курсор к строке поиска, посимвольно набирает `Magento developer in <City>`, запускает поиск и кликает первый результат. Всё это записывается на видео с визуальными эффектами.

## Быстрый старт

Требуется Python 3.9+ и доступ в интернет. Сторонних зависимостей, кроме `playwright`, нет.

```bash
git clone <URL репозитория> csv-video-recorder
cd csv-video-recorder

python -m venv .venv
.venv\Scripts\activate              # Windows (PowerShell / cmd)
# source .venv/bin/activate         # Linux / macOS

pip install -r requirements.txt
python -m playwright install chromium

python capture_video.py             # или: python capture_video.py path/to/other.csv
```

Откроется окно Chromium, сценарий выполнится для каждой строки `input.csv` (~20 c на строку). Готовые видео — в папке `video/`.

## Возможности

- **SVG-курсор** — белая стрелка с чёрной обводкой (32×32) поверх страницы; Playwright не записывает системный курсор.
- **Жёлтая подсветка** — полупрозрачный круг (r ≈ 32 px) под стрелкой, следует за курсором.
- **Click ripple** — волна от точки каждого клика.
- **Callout** — жёлтая плашка с текстом 16pt и плавным появлением/исчезанием (~2 c).
- **Плавное движение и «человеческий» ввод** — без телепортов курсора, случайные задержки между символами.
- **Устойчивость** — ошибка в одной строке CSV не останавливает остальные.

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

Видео (1280×720) сохраняются как `./video/<Company Name>.webm`. Недопустимые для имени файла символы заменяются на `_`, а существующий файл с тем же именем перезаписывается.

## Формат входного CSV

UTF-8 (BOM, который добавляет Excel, допускается), разделитель `;`, кавычки `"`. Заголовок обязателен и должен совпадать точно:

```text
URL;Company Name;Language;Country;Postcode;City;Street;HouseNo
"https://duckduckgo.com/";"Acme Magento Minsk";"ru";"Belarus";"220030";"Minsk";"Nezavisimosti Ave";"4"
```

В сэмпл-сценарии используются `URL`, `City` (в поисковом запросе) и `Company Name` (имя видео). Остальные колонки передаются в функцию и пишутся в лог.

## Настройки

| Что | Где | По умолчанию | Описание |
|-----|-----|--------------|----------|
| `PATH_TO_VIDEO_FOLDER` | `qscript.py` (переопределяется в `capture_video.py`) | `./video` | Папка для видео; создаётся автоматически |
| `HEADLESS` | `qscript.py` | `False` | Запуск браузера без окна. DuckDuckGo надёжнее работает с видимым окном |
| `VIDEO_SIZE` | `qscript.py` | 1280×720 | Viewport и размер видео |
| `RESULTS_TIMEOUT_MS` | `qscript.py` | `30000` | Ожидание результатов поиска |
| `QSCRIPT_LOG_LEVEL` | переменная окружения | `INFO` | `DEBUG` / `INFO` / `WARN` / `ERROR` |

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
- `ShowCallout(text, bgColor="#FFFF00", textColor="#000000", fade=True, page=None)` — показывает плашку с текстом. Без `page` использует страницу текущего сценария, поэтому вызывается изнутри `CaptureVideo_Magento`.

## Устранение неполадок

| Симптом | Решение |
|---------|---------|
| `Failed to build installable wheels ... greenlet` при `pip install` | У свежей версии `greenlet` нет готового wheel для вашей версии Python, а компилятора C++ нет. Используйте `requirements.txt` из репозитория (в нём `--prefer-binary`) или выполните `pip install --prefer-binary -r requirements.txt` |
| `[ERROR] CSV header must be: ...` | Заголовок CSV должен совпадать точно: `URL;Company Name;Language;Country;Postcode;City;Street;HouseNo`, разделитель `;` |
| `[ERROR] cannot write ... PermissionError` | Видео с тем же именем открыто в плеере — закройте его и запустите снова |
| `[WARN] row N overwrote the video of row M` | В CSV повторяется `Company Name`: видео сохраняется как `<Company Name>.webm`, поэтому последняя строка перезаписывает предыдущую |
| `Executable doesn't exist ... chromium` | Выполните `python -m playwright install chromium` |
| `FAILED: <company>` и `results did not appear` | DuckDuckGo показал капчу или медленно отрисовывает выдачу: повторите запуск, оставьте `HEADLESS = False`, увеличьте `RESULTS_TIMEOUT_MS` |
| Ошибка вида `filter() got an unexpected keyword argument 'visible'` | Обновите Playwright: `pip install -U "playwright>=1.51"` |
| На Linux-сервере нет дисплея | Запускайте через `xvfb-run python capture_video.py` или установите `HEADLESS = True` |
| Нужны подробности шагов | `QSCRIPT_LOG_LEVEL=DEBUG` — координаты курсора, точки кликов, ожидания |

## Структура

| Файл | Назначение |
|------|------------|
| `capture_video.py` | Точка входа: читает CSV через `csv.DictReader(f, delimiter=";", quotechar='"')` и вызывает сценарий для каждой строки |
| `qscript.py` | `PATH_TO_VIDEO_FOLDER`, `CaptureVideo_Magento`, `ShowCallout`, JS-оверлеи курсора |
| `requirements.txt` | `playwright>=1.51` (с `--prefer-binary` для установки на Python 3.9 без компилятора) |
| `input.csv` | Пример входных данных |
