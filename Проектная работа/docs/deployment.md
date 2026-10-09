# Развёртывание и обслуживание

Настройка окружения, запуск Docker Compose, обновление и команды проверок.

## Запуск

Окружение: Python 3.11+, Docker Engine с Compose v2 (Ubuntu) или Docker Desktop
(macOS/Windows). Первый запуск требует доступа к GitHub, Docker Hub и Go-модулям:
MinIO собирается из закреплённых исходников. Модель эмбеддингов около 220 МБ
загружается при первом обращении и сохраняется в volume.

Первый запуск из корня репозитория. Дальнейшие команды выполняются из `mvp`:

```bash
cd mvp
python3 scripts/init_env.py
```

Скрипт создаёт `.env` с несекретными параметрами и `secrets/` с паролями и токенами.
Повторный запуск сохраняет существующие значения. Каталог имеет права `0700`,
файлы - `0644` для чтения непривилегированными процессами контейнеров.
Оба пути исключены из Git. Секреты не включаются в образ.

Модельный профиль в `.env`:

```dotenv
MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL=qwen/qwen3.7-flash
VISION_MODEL=qwen/qwen3.7-flash
MODEL_REASONING_EFFORT=none
```

Ключ провайдера хранится одной строкой в `secrets/model_api_key`, без имени
переменной и кавычек. При ручной замене файла доступ контейнера обеспечивает
`chmod 644 secrets/model_api_key`. Без ключа интерфейс запускается, но обработка
документов завершается ошибкой.

```bash
docker compose up -d --build
docker compose ps
```

Интерфейс: <http://127.0.0.1:8080>. Вход - по токену из
`secrets/accountant_token` или `secrets/reader_token`. Бухгалтер видит `shared`
и `finance`, читатель - только `shared` и не имеет права загрузки.
Один пакет принимает до 10 файлов общим размером до 50 MiB. Один файл - до
10 MiB и 12 страниц. Лимиты задаются в `.env`.

Диагностика из каталога `mvp`:

```bash
docker compose logs --tail=100 api
curl -fsS http://127.0.0.1:8080/api/v1/health/live
```

Эмбеддинги по умолчанию локальные, размерность 384. Удалённый режим задаётся через
`EMBEDDING_BACKEND=remote`, `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL` и размерность.
Ключ хранится в `secrets/embedding_api_key`. Размерность существующей коллекции
Qdrant фиксирована. Изменение предполагает новую коллекцию и повторную индексацию.

## Проверки

Команды ниже выполняются из `mvp`. Локальное окружение для скриптов и тестов:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pytest -q tests -c pytest.ini
node --check app/static/app.js
```

Последняя команда использует Node.js для проверки синтаксиса интерфейса.
Для запуска самого приложения Node.js не нужен.

Сквозная проверка с моделью загружает документы из каталога
`mvp/documents/`:

```bash
.venv/bin/python scripts/e2e_smoke.py
```

Сканы и их группы доступа перечислены в `mvp/documents/manifest.json`.
Генератор `make_realistic_scans.py` пересоздаёт набор. `relink_contracts.py` восстанавливает
связи уже загруженных документов без вызова модели.

Проверки реальных хранилищ без LLM и нагрузочный замер чтения:

```bash
docker compose cp scripts/check_indexes.py api:/tmp/check_indexes.py
docker compose exec -T -w /app api env PYTHONPATH=/app python /tmp/check_indexes.py
docker compose exec -T api python scripts/check_persistence.py
.venv/bin/python scripts/load_test.py --duration 15 --concurrency 4
```

Экспорт трасс в Jaeger включается параметром
`OTEL_TRACES_ENDPOINT=http://jaeger:4318/v1/traces` в `.env`:

```bash
docker compose --profile observability up -d --build
```

Jaeger доступен на <http://127.0.0.1:16686>. Без OTLP-адреса spans выводятся в лог.

## Серверный доступ и обновление

Базовый Compose публикует API только на localhost. Доступ с другого компьютера
обеспечивает SSH-туннель `ssh -L 18080:127.0.0.1:8080 user@SERVER_IP`,
после чего интерфейс доступен на <http://localhost:18080>.

HTTP-доступ по IP для демонстрации (Compose v2.24.4+, входящий TCP-порт 8080):

```bash
docker compose -f compose.yaml -f compose.ip.yaml up -d --build
```

Адрес - `http://SERVER_IP:8080`.

После `git pull` обновление выполняется командой:

```bash
docker compose -f compose.yaml -f compose.ip.yaml up -d --build api
```

Данные сохраняются в томах при обновлении контейнеров. Команда
`docker compose down -v` удаляет тома вместе с данными. Резервная копия включает
PostgreSQL, MinIO, индексы и `secrets/`. Автоматическая процедура восстановления не реализована.
Ротация секрета включает замену файла и пересоздание контейнера. Пароль
инициализированной базы меняется также в самой БД. Для Neo4j значения в
`neo4j_password` и `neo4j_auth` совпадают.
