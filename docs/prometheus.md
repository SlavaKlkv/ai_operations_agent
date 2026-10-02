# Подключение Prometheus

AI Operations Agent может проверять доступность одного пользовательского
Prometheus через его HTTP API. Это read-only подключение: приложение вызывает
только `/api/v1/query_range`, `/api/v1/alerts` и `/-/ready`; оно не меняет
правила, targets или данные Prometheus.

## Настройка

В пользовательском окружении укажите адрес инстанса, доступного контейнеру:

```dotenv
PROMETHEUS_URL=http://host.docker.internal:9090
PROMETHEUS_SERVICE_LABEL=service
```

На Linux вместо `host.docker.internal` используйте адрес вашего Prometheus или
настройте DNS/маршрут, доступный контейнеру. Адрес не должен содержать путь,
query-параметры или логин с паролем: базовый продукт не хранит секреты
мониторинга в `.env` и не проксирует произвольные запросы.

`PROMETHEUS_SERVICE_LABEL` — label, по которому Prometheus различает сервисы.
По умолчанию это `service`; например, если метрики размечены
`app="billing-service"`, укажите `PROMETHEUS_SERVICE_LABEL=app`.

## Поддерживаемые метрики

Подключение использует фиксированный набор PromQL-шаблонов, чтобы задача
агента не могла превратиться в неограниченный консольный доступ к Prometheus:

- `error_rate` — доля 5xx по `http_requests_total`;
- `request_rate` — скорость запросов по `http_requests_total`;
- `latency_p50`, `latency_p95`, `latency_p99` — квантили гистограммы
  `http_request_duration_seconds_bucket`;
- активные alerts из `/api/v1/alerts`.

Если в вашей системе имена метрик отличаются, источник будет показываться как
подключённый, но запрошенная метрика вернёт пустой ряд. Пользовательский
редактор схем метрик и любые write-операции Prometheus не входят в первую
версию.

## Проверка

После перезапуска приложения откройте мастер: строка `Prometheus` должна
показать «Реальный Prometheus доступен для read-only запросов». Если проверка
не проходит, проверьте URL, сетевую доступность контейнера и endpoint
`/-/ready` на стороне Prometheus.
