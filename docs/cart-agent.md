[← Объяснение лабораторной](LAB_EXPLANATION.md) · [К README](../README.md)

# Task 4: корзина Magento и screenshot checkout

`magento_cart_agent.py` принимает текстовый список URL и исследует неизвестные магазины через Browser-Use и DeepSeek. Агент сам выбирает путь от главной, категории или товара до checkout. Правил по именам сайтов и ветвления Python по типу начальной страницы нет.

Платформу, тип страницы, способ закрытия баннеров, обязательность входа и готовность формы доставки определяет ИИ по фактическому сайту. Python отвечает за браузер/CDP, контроль явных признаков Cloudflare, независимое чтение корзины и сохранение PNG/JSON. В коде нет второго распознавателя формы по CSS, labels, autocomplete или списку HTML-контейнеров.

Для каждого URL скрипт запускает собственный Chromium с persistent context и передаёт его адрес Chrome DevTools Protocol (CDP) в `Browser(cdp_url=..., keep_alive=True)`. Playwright и Browser-Use управляют одним браузером, cookies и localStorage общие. База данных и запущенный Flask-сервис этому CLI не нужны.

## Установка на компьютере

Нужен Python **3.11+**; проверенное окружение — Python 3.12. Существующие инструменты Tasks 1–3 сохраняют совместимость с Python 3.9. Для Task 4 версии зафиксированы: `browser-use==1.0.6`, `python-dotenv==1.2.4`.

Windows PowerShell, из корня проекта:

```powershell
py -3.12 -m venv .venv-task4
.venv-task4\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
```

Linux/macOS: создайте окружение через `python3.12 -m venv .venv-task4`, активируйте `source .venv-task4/bin/activate`, затем выполните те же команды установки зависимостей и Chromium. На Linux общий requirements также устанавливает mod_wsgi, которому нужны Apache headers/compiler, как в Dockerfile проекта.

Укажите `DEEPSEEK_API_KEY` только в локальном `.env` или окружении. При новом клоне можно скопировать `.env.example` в `.env`; существующий файл редактируйте без перезаписи ключа. Task 4 не меняет `DEEPSEEK_MODEL` Task 3: его модель задаёт `MAGENTO_MODEL`.

```bash
python magento_cart_agent.py --urls urls.txt
```

Если managed Chromium не установлен, используйте установленный браузер:

```bash
python magento_cart_agent.py --urls urls.txt --channel msedge
python magento_cart_agent.py --urls urls.txt --channel chrome --headless
```

Это альтернативные примеры, каждый запускает отдельный batch с платными запросами DeepSeek. `--channel` и `--executable-path` взаимоисключающие. Нет автоматического перехода на облачный браузер или другого платного провайдера.

## Вход и результат

В `urls.txt` один URL на строку; пустые строки и строки с `#` пропускаются. Допустим UTF-8 BOM. К bare domain добавляется `https://`. Принимаются http/https URL без встроенных credentials; невалидная строка получает `invalid_url`, остальные продолжают выполняться.

По умолчанию обрабатывается весь файл. `--stop-on-success` останавливает batch после первой **независимо подтверждённой корзины**, даже если форму не удалось найти или сохранить. Полный результат `success` дополнительно требует сообщения агента о достигнутой форме доставки и подтверждения конкретного сохранённого PNG через `screenshot_id`, без блокировки, timeout или ошибки.

Агент закрывает overlays, выбирает товар и обязательные опции, добавляет его и вызывает `verify_magento_cart` как tool/function call. Инструмент не получает injected page/browser: он подключается по CDP и читает `localStorage['mage-cache-storage']`. Корзина непустая при положительном summary_count или непустых items/items_count. После окончания агента скрипт заново читает браузер независимо от слов модели и ответа tool.

Tool и итоговое чтение корзины предпочитают выбранный агентом CDP target, если его host входит в scope текущего сайта. Это исключает подмену корзиной другой вкладки/поддомена при неоднозначном `hasFocus()` в headless Chromium. При недоступном target или внешнем checkout остаётся выбор только среди вкладок текущего сайта; диагностика использует `[FIX:cart-target]`.

Дробный `summary_count` сохраняется без округления: `0.5` также означает непустую корзину. Агент возвращает платформу и наблюдённые признаки в структурированном результате. Python сохраняет этот вывод без требования дословно найти цитату в основном DOM: сведения могут относиться к фрейму, компоненту или другому представлению страницы. Отсутствие Magento key само по себе не доказывает другую платформу. Структурированный ответ не заменяет независимое чтение корзины; достоверно прочитанный Magento cart может определить Magento при недостатке сведений модели.

На подтверждённом Magento агент идёт в checkout, проверяет, что форма доставки действительно открыта, не закрыта баннером и не заполнена, прокручивает её в область просмотра и оставляет страницу для снимка. Заполнять её, создавать аккаунт или оформлять заказ задача не разрешает. Агент сам разбирается с конкретной разметкой, включая фреймы и компоненты, и сообщает `checkout_reached`; при CAPTCHA или обязательном входе сообщает blocker и прекращает сценарий.

Перед завершением агент вызывает `preview_checkout_screenshot`: Python заново проверяет корзину, сохраняет PNG и передаёт его модели как изображение вместе с `screenshot_id` (SHA-256 байтов). Агент должен визуально проверить именно этот PNG: email/contact, все адресные поля, пустые значения и отсутствие перекрытий. Для длинной формы используется `full_page=true`; `false` допустим, если вся форма помещается в viewport. При перекрытии липкой шапкой агент корректирует прокрутку и повторяет preview. Закрываемые chat/feedback/debug/performance-виджеты нужно закрыть обычными кнопками, включая свёрнутые панели вне области формы. Оставшиеся дефекты агент сообщает в `visual_issues`; непустой список исключает `success`, даже если модель одновременно сообщила о готовности формы. Обычные фиксированные элементы страницы сами по себе не считаются overlays. Скрывать шапки/поля через CSS/DOM, подделывать Magento storage или добавлять дубликаты ради обновления счётчика запрещено промптом. После удовлетворительного preview агент возвращает его `screenshot_id` и сразу завершает работу. Визуальные решения остаются у модели; отдельного Python-распознавателя формы нет.

После независимого подтверждения корзины Python выполняет §8 ТЗ: подключается по CDP и ищет страницу с URL вида checkout/onepage/kasse/cassa, предпочитая текущий сайт; checkout другого домена допускается как fallback. Поиск адресной формы вне этих URL и отдельная проверка CSS/пикселей в Python не выполняются. Ответственность за готовность интерфейса остаётся у агента.

Если выбранный агентом CDP target доступен, снимок делается именно с этой checkout-вкладки: старая корзина не должна подменять страницу, о которой сообщил агент. При недоступности target остаётся предусмотренный §8 поиск по URL с предпочтением текущего сайта; причина записывается в DEBUG-лог.

PNG снимает скрипт: `checkout_screenshots/<NN>_<domain>.png`, где NN — номер URL в списке. Каждый preview заменяет предыдущий снимок; неудачная новая попытка сбрасывает прежнее подтверждение. После окончания агента Python сохраняет тот же PNG без повторной съёмки, проверяет его SHA-256 и совпадение возвращённого моделью `screenshot_id`. Изменённый после preview файл даёт `screenshot_failed`; устаревший/отсутствующий ID не даёт `success`. Если preview не получен, скрипт может сохранить диагностический screenshot найденного checkout URL, но он не считается просмотренным. Повторный запуск с тем же каталогом может перезаписать одноимённый PNG; для отдельных прогонов задавайте разные `--screenshot-dir` и `--output`.

Preview дополнительно привязан к выбранному CDP target и основному документу: frame ID, loader ID, URL и fragment проверяются до и после съёмки и ещё раз при финализации. Навигация во время съёмки не создаёт receipt. Навигация, reload, смена target или route после preview оставляет `checkout_not_reached`, `screenshot_current=false` и диагностическое сообщение; старый PNG может сохраняться как свидетельство предыдущего состояния. Недоступность target также не считается успешной проверкой. Это проверка навигационной идентичности, а не независимое распознавание изменений интерфейса внутри того же документа.

Выбранная внешняя вкладка сама по себе не считается checkout: рекламная страница агентства или документация могут иметь собственную Cloudflare-защиту. Наблюдатель учитывает внешний checkout-маршрут и сохраняет связь вкладки с ним при дальнейших challenge-редиректах. Промпт требует возвращаться в исходный магазин после случайного перехода на постороннюю рекламную страницу, а не переносить её платформу или блокировку на магазин.

JSON сохраняется в `magento_results.json`, с checkpoint после каждого URL и в конце. Обязательные поля: `url`, `platform`, `cloudflare`, `status`, `agent_success`, `error`, `final_result`, `cart`, `screenshot`. Дополнительно записываются baseline, evidence, blocker, agent_status, cart_verified, checkout_reached и checkout_url. `agent_checkout_reached`, `agent_blocker` и `agent_blocker_evidence` отделяют наблюдения модели от независимых фактов Python. `agent_screenshot_id` — подтверждение модели, `screenshot_id` — SHA-256 последнего preview, `screenshot_reviewed` — их совпадение при неизменном файле и актуальном документе, `screenshot_mode` — `full_page` или `viewport`. `screenshot_current` отражает проверку target/document, `screenshot_error` хранит отдельную диагностику снимка, `agent_visual_issues` — замечания модели к последнему PNG. Это не программное доказательство видимости формы. `cart_verified` основан на новом чтении localStorage; `checkout_reached` требует готовности формы от агента, подтверждённого актуального PNG и отсутствия визуальных замечаний. Путь screenshot появляется только после успешной записи файла.

Если при timeout или основной технической ошибке агента дополнительно не удалось сохранить/проверить PNG, исходные `status` и `error` сохраняются. Вторичная причина записывается в `screenshot_error`, а не подменяет первоначальную ошибку.

| status | Значение |
|---|---|
| success | Корзина независимо подтверждена, агент сообщил о готовой форме доставки и подтвердил точный сохранённый PNG |
| platform_mismatch | Обнаружена другая платформа; Magento storage для неё не требуется |
| blocked | Cloudflare, CAPTCHA или обязательный login wall; причина в blocker/markers |
| cart_failed | Magento определён, независимое чтение не подтвердило товар |
| platform_unknown | Недостаточно признаков платформы и нет подтверждённой Magento-корзины |
| checkout_not_reached | Корзина подтверждена, но нет готовности формы, актуального подтверждения PNG или самого checkout PNG; screenshot может быть диагностическим |
| screenshot_failed | Ошибка сохранения PNG; cart_verified может быть true |
| timeout / error / invalid_url | Лимит, техническая ошибка или невалидный вход |

`agent_success` — собственный вердикт модели, он может отличаться от итогового статуса. Даже `agent_success=true` и `agent_checkout_reached=true` не заменяют независимую корзину и реально созданный PNG. Timeout и техническая ошибка сохраняются независимо от найденной корзины или screenshot. Код выхода CLI 0 означает, что batch записан, а не что каждый сайт прошёл checkout; 1 — ошибка конфигурации/ввода/записи, 2 — ошибка argparse, 130 — прерывание пользователем.

## Настройки

Приоритет: CLI → уже заданный env → выбранный `.env` → defaults. Сначала читается `--env-file` (или `MAGENTO_ENV_FILE`, default `.env`), затем вычисляются argparse defaults. Значения существующего env файл не перезаписывает.

| CLI | Env | Default / назначение |
|---|---|---|
| --urls | MAGENTO_URLS | urls.txt |
| --llm | MAGENTO_LLM | auto; поддерживаются auto/deepseek |
| --model | MAGENTO_MODEL | deepseek-flash, передаётся как deepseek:deepseek-flash |
| --api-key | DEEPSEEK_API_KEY | Обязательный ключ; предпочтительнее env/.env |
| --max-steps / --timeout | MAGENTO_MAX_STEPS / MAGENTO_TIMEOUT | 50 шагов / 180 секунд на Agent.run |
| --headless | MAGENTO_HEADLESS | false |
| --stop-on-success | MAGENTO_STOP_ON_SUCCESS | false |
| --cdp-port | MAGENTO_CDP_PORT | 0: свободный локальный port |
| --channel / --executable-path | MAGENTO_CHANNEL / MAGENTO_EXECUTABLE_PATH | Managed Chromium, либо Chrome/Edge/путь |
| --profile-dir | MAGENTO_PROFILE_DIR | Временный профиль, удаляется после URL |
| --proxy | MAGENTO_PROXY | scheme://[user:pass@]host[:port], browser proxy |
| --user-agent | MAGENTO_USER_AGENT | Default Chromium UA; viewport 1280×900 |
| --cookies / --stealth-js | MAGENTO_COOKIES / MAGENTO_STEALTH_JS | Cookies JSON и дополнительный init script |
| --screenshot-dir / --output | MAGENTO_SCREENSHOT_DIR / MAGENTO_OUTPUT | checkout_screenshots / magento_results.json |

`MAGENTO_LOG_LEVEL` задаёт DEBUG (default), INFO, WARNING или ERROR. Пример: `[INFO] [cart-agent] Custom tool verify_magento_cart invoked`. DEBUG показывает CDP lifecycle и изменения наблюдаемых признаков; ERROR — причину технического сбоя. Логи Browser-Use отдельно управляются `BROWSER_USE_LOGGING_LEVEL`.

Независимые обращения к странице ограничены 5 секундами, целиком фаза чтения корзины или поиска/capture checkout — 20 секундами; отключение CDP имеет отдельный cleanup deadline. Это отдельные пределы от `--timeout` для Agent.run. Зависший JavaScript становится диагностической ошибкой, а не доказанной пустой корзиной. Исправленные проверки пишут `[FIX:browser-io]`, `[FIX:active-tab]`, `[FIX:checkout]` и `[FIX:proxy-bypass]`.

Встроенный stealth скрывает webdriver и задаёт languages. Cookies JSON — список объектов формата Playwright `context.add_cookies`. Browser proxy отличается от HTTP_PROXY/HTTPS_PROXY/ALL_PROXY для API-клиента. Если в окружении задан SOCKS-прокси для API, установите его optional transport: `python -m pip install "httpx2[socks]"`. На этой машине именно отсутствие SOCKS transport сначала давало вводящую в заблуждение ошибку Browser-Use о DeepSeek extra; отдельный DeepSeek extra не понадобился.

## Проверки

Без API и без внешних магазинов:

```bash
python scripts/verify_magento_cart.py
python scripts/verify_magento_cart.py --channel msedge
```

Это альтернативные команды: первая использует managed Chromium, вторая Edge. Запускаются offline-тесты и настоящий CDP smoke с pinned Browser-Use, общим storage, несколькими host и screenshot локальной формы. Тесты не требуют pytest-asyncio. Установленный browser обязателен; skip не означает приёмку.

Явно платная проверка:

```bash
python scripts/verify_magento_cart.py --live --urls urls.txt --channel msedge --max-steps 30 --timeout 120
```

Runner не перезапускает агента после неуспеха. Внутри одного Agent.run библиотека может выполнять свои ограниченные восстановительные шаги. Результаты сохраняются в `magento_results_live.json`; валидный JSON без success не означает полной приёмки.

Автотесты проверяют разделение ответственности: ответ модели не подменяет independent cart read, сообщение о незавершённом checkout не становится успехом из-за существующего PNG, явный Cloudflare marker останавливает run, а screenshot выбирает checkout URL с предпочтением текущего сайта. Контрактные проверки не требуют Python-распознавания отдельных вариантов DOM/CSS. Рабочая вкладка наблюдателя определяется по выбранному CDP target демона Browser-Use 1.0.6; `hasFocus()` остаётся резервной эвристикой при отсутствии подключённого агента. Внешний checkout предоставляет признаки блокировки, но не расширяет host scope корзины. DB suites запускаются последовательно: они используют одну тестовую БД.

### История проверок до коррекции ответственности

Приведённые ниже количества тестов и результаты относятся к предыдущим версиям. Проверки Python-эвристик видимости и распознавания формы заменяются проверками контракта агента; старые числа не подтверждают текущую реализацию. Для новой версии остаются обязательными реальный Magento run с независимо подтверждённой корзиной и визуальный просмотр PNG незаполненной формы.

При первоначальной приёмке 22 ноября 2026 прошли 43 unittest и 263 сервисных pytest; live-результаты того прогона приведены ниже. После исправлений отдельно повторён samenfest.de: независимая корзина подтверждена, PNG пустой формы доставки просмотрен (`magento_results_fix.json`).

Повторный прогон после исправлений 23 ноября 2026 дал тот же результат на шести sample-сайтах: четыре success, Shopify platform_mismatch и Cloudflare blocked (`magento_results_fix_20261123.json`). Дополнительный Edge-прогон со страницы категории samenfest.de/tomatensamen.html завершился success; `--stop-on-success` сохранил только первый URL (`magento_results_fix_category_20261123.json`, CLI exit 0). Пять PNG этих прогонов просмотрены: формы видимы и пусты. Итоговые проверки: 70 тестов Task 4 в managed Chromium, 69 в Edge до добавления последней offline-регрессии, 271 сервисный pytest и 43 unittest; Docker build прошёл.

| Реальный сайт | Наблюдённый результат |
|---|---|
| rupp-onlineshop.de | success, корзина + PNG пустой shipping form |
| samenfest.de | success, корзина + PNG пустой shipping form |
| schmuckshopping.de | success, корзина + PNG пустой shipping form |
| senso-care.de | success, корзина + PNG пустой shipping form |
| schoen-und-wieder.de | platform_mismatch, Shopify |
| semilac.de | blocked, HTTP 403 Server: cloudflare |

Все четыре PNG просмотрены. Отдельный live-run `--stop-on-success` обработал только первый URL. На дополнительном, не входящем в samples, `magento2demo.firemultimedia.nl` агент определил Magento, но корзина осталась пустой: сохранён `cart_failed`. Это подтверждает честную обработку неуспеха, не гарантирует успех на любом новом магазине. Результаты, screenshots и runtime-логи исключены из Git и Docker context.

## Ограничения и диагностика

Исторически последующий fix проверялся 77 тестами Task 4, 271 сервисным pytest и 43 unittest; Docker build прошёл. Тогда дополнительно повторялся Magebit через DeepSeek/Edge: success, независимая корзина и PNG пустой формы доставки (`magento_results_fix_visibility_i18n.json`, CLI exit 0). Это evidence той версии, а не замена новой live-приёмки.

- Cloudflare фиксируется до агента и во время его работы; runner отменяет run при обнаружении блокировки. Проверяются вкладки текущего сайта и связанные внешние checkout текущего прогона, включая последующие challenge URL. Старые посторонние вкладки профиля не расширяют область наблюдения. В headless Playwright сразу несколько вкладок могут сообщать hasFocus=true, поэтому активный target берётся у демона Browser-Use. HTTP 403 Server cloudflare сохраняется даже при последующем закрытии документа. По ТЗ cookie __cf_bm/cf_clearance считается marker blocked; cf_clearance может существовать и после успешной проверки, поэтому эвристика консервативная. CAPTCHA не решается, proxy не ротируется для обхода.
- Платформу и смысл интерфейса определяет агент по наблюдаемым признакам; его evidence сохраняется отдельно. Python не подтверждает это решение поиском точной цитаты в основном DOM. Отсутствие Magento key само по себе не доказывает другую платформу.
- Готовность формы, закрытие overlays и обязательность входа — наблюдения агента. Python не гарантирует видимость формы по CSS или пикселям: окончательная live-приёмка включает визуальную проверку PNG. Диагностический screenshot не следует принимать за подтверждение завершения checkout.
- Корзина читается только с host текущего сайта, с допуском www/поддоменов. Старая чужая вкладка не подтверждает корзину. Допустимый по §8 screenshot внешнего checkout не ослабляет эту изоляцию чтения localStorage по §4.
- Для собственного `--profile-dir` используйте выделенный каталог. Он сохраняется, включая старую корзину; baseline помогает диагностике, но положительный count сам по себе не доказывает новое добавление. Для чистой приёмки используйте default временный профиль.
- Недоступный CDP/browser, malformed storage и filesystem errors диагностируются отдельно от пустой корзины. Ошибка одного URL не прерывает следующие; ошибки записи итогового JSON возвращают ненулевой exit code.
- На Linux нужны библиотеки запуска Chromium; на Windows при невозможности скачивания managed browser используйте установленный Chrome/Edge. Для устаревшего Python CLI требует 3.11+, не меняя старые инструменты.

## См. также

- [Компании и Impressum](companies.md) — Task 3 и общий DeepSeek key.
- [Зум и API](zoom.md) — независимый pipeline видео Tasks 1–2.
- [README](../README.md) — общая установка и веб-сервис.
