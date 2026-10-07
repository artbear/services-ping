# Проверка первого выпуска

Дата: 2026-10-07. Локальная среда: Linux, Python 3.12.3, Node.js 22.23.2,
Chromium через Playwright 1.63.0. Обязательных зависимостей Python нет.

## Воспроизводимые проверки

```sh
python3 -m unittest discover -s tests -v
npm ci
npx playwright install chromium
npm run test:browser
```

Python suite: 28 тестов. Проверяются API/CSRF/Host/Origin, импорт и сохранность
данных, rollback при отказе сохранения, cleanup и retention, ограничение
HTTP workers и stalled clients, реальные локальные HTTP/TCP probes,
deadline отдельного процесса, редиректы, классификация ответов,
ограничение service concurrency, поколения результатов, ручной цикл в паузе,
пауза при автоматической отправке, notification transitions/cooldown,
валидация HTTP/TCP/ICMP и адаптеры ICMP Linux/macOS/Windows.

Браузерный smoke пройден в Chromium с viewport 1440×1000 и 390×844:
CRUD, безопасный вывод имени с HTML, отключение цели, offline/partial/recovery,
пауза и ручная проверка, история с null/missing RTT без ложных 0 мс,
настройки, экспорт/импорт с подтверждением, удаление,
отдельный баннер потери связи с локальным backend. Нет JavaScript errors
или горизонтального переполнения. Screenshots сохраняются в игнорируемый `.work/`.

## Реальный запуск

`python3 -m services_ping --port 18765 --data-dir /tmp/services-ping-live-final`
проверен на Linux с доступом к сети. Опорные адреса: Google 204,
Cloudflare 200, Яндекс robots.txt 200; итоговое состояние `online`.
Начальные сайты Google/Яндекс дали 200. Публичные LLM endpoints дали обычные
HTTP-ответы и отображались как `reachable` с неизвестным здоровьем API.
Платные запросы и ключи не использовались.

Это наблюдение в конкретной сети, а не гарантия доступности этих адресов
в другой стране или через другой VPN. Настройте собственные VPS/VPN/BB цели
в интерфейсе: личные адреса не были предоставлены и не включены в Git.

## Платформенные пределы

GitHub Actions содержит матрицу Python 3.10/3.12 на Linux/Windows/macOS и
Chromium smoke на Linux. Она не заменяет проверки реального системного ping
и разрешений уведомлений на пользовательском ноутбуке.

Физический Android/Termux пока не проверен; поддержка Android v1 — preview.
Doze, остановка процесса и ограничения производителя могут прервать мониторинг.
APK, foreground service и запуск после перезагрузки в этот выпуск не входят.
Чеклист ручной проверки и инструкции Termux находятся в README.md.

Независимое ревью плана и кода сохранено в PLAN_REVIEW.md и CODE_REVIEW.md.
