# Studio Bot 0.7.4 — первый запуск

Распакуйте **весь ZIP** в обычную папку. Не запускайте файлы внутри окна архива.
Для Windows нужен Python **3.11 или новее**. Именно 3.13 устанавливать не требуется.
Docker нужен только при выборе контейнерного запуска на сервере.

## Windows

1. `Verify.cmd` проверяет полноту распаковки и контрольные суммы, без установки библиотек.
2. `Install.cmd` создаёт `.venv`, устанавливает зависимости, создаёт `data`, `secrets` и шаблон `.env`.
3. `Setup.cmd` запрашивает токен одного бота и новый пароль админки. Ввод секретов скрыт.
4. Отдельно получите у владельца JSON-ключ Google и сохраните как `data/google-service-account.json`.
5. `Check.cmd` проверяет настройки, ключ, фотографии и возможность записи в `data`.
6. `CheckGoogle.cmd` проверяет реальное чтение подключённых таблиц.
7. Остановите старую копию бота, затем откройте `Start.cmd`. В Telegram: `/start` или `/admin`.

Если Python не найден, установите его и откройте установщик заново. Python Launcher необязателен:
проверяются `py -3`, `python`, `python3`. Для ручного выбора можно запустить
`"C:\путь\python.exe" bootstrap.py install`. При повторном запуске используется `.venv` проекта.
Не переносите `.venv` между компьютерами: создайте её заново через Install.

## Linux без Docker

```sh
python3 bootstrap.py verify
python3 bootstrap.py install
python3 bootstrap.py setup
# Положите отдельный ключ Google в data/google-service-account.json.
python3 bootstrap.py check
python3 bootstrap.py check-google
python3 bootstrap.py run
```

## Linux с Docker

Нужны Docker Engine и Compose **2.24+**. Python на хосте не требуется.

```sh
sh Server.sh setup
# Получите ключ Google отдельно, сохраните в secrets/google-service-account.json.
# Контейнеру UID 10001 нужен доступ к ключу, например:
sudo chown 10001:10001 secrets/google-service-account.json
sudo chmod 600 secrets/google-service-account.json
sh Server.sh check
# Остановите прежнюю копию бота перед запуском новой.
sh Server.sh start
```

`sh Server.sh stop` останавливает бот; `sh Server.sh logs` показывает последние сообщения.
Обычные команды `docker compose` также поддерживаются, подробности в README.md.
Python 3.13 в Dockerfile — базовый образ контейнера; версия Python на компьютере с ним не связана.

## Что нужно передать отдельно

ZIP содержит код, фотографии, настройки комнат/таблиц и скрытые **шаблоны**.
Внутри нет рабочего токена, пароля, ключа Google и истории сотрудников.
Пустая новая база создаётся автоматически при первом запуске.

- **Новая установка:** токен от владельца → Setup → отдельный ключ Google.
- **Перенос действующего бота:** остановить прежнюю копию, отдельно перенести `.env`, ключ Google
  и папку `data` (для Docker — восстановить базу в том, см. README). Сохранить `studio.toml`.
- `.env.example` — шаблон, он не заменяет выданные владельцем секреты.
- Не включайте секреты в общий ZIP. Связь Google Drive в чате не даёт серверу доступ к таблицам.

Не запускайте две копии с одним токеном. Обычные установка и запуск не очищают историю.
