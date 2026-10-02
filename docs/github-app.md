# Общая GitHub App для локального приложения

Этот файл описывает конфигурацию разработчика, а не действие каждого пользователя.
Пользователь подключается через GitHub Device Flow без PAT, client secret и собственного
публичного сервера. Пока общая App не зарегистрирована, мастер честно показывает, что
GitHub не настроен, и допускает только demo-режим.

## Регистрация

1. Зарегистрировать **public GitHub App** с уникальным slug и HTTPS homepage проекта.
2. Включить **Enable Device Flow** и истекающие user access tokens.
3. Не включать OAuth-авторизацию при установке и webhook: локальный контейнер не может
   принимать callback или webhook с GitHub. Callback URL и webhook URL не нужны для
   Device Flow.
4. Выдать минимальные repository permissions для реализованных read-запросов:
   `Metadata: read`, `Contents: read` (коммиты), `Pull requests: read` и
   `Deployments: read`. `Actions: read` понадобится только после подключения
   workflow runs. `Issues: write` нужно лишь для будущей подтверждённой записи issue;
   до включения этой возможности разрешение не запрашивать.
5. Включить установку только на выбранные пользователем репозитории. Для проверки
   использовать отдельный тестовый аккаунт и репозиторий.
6. Добавить публичные `GITHUB_APP_CLIENT_ID` и `GITHUB_APP_SLUG` в конфигурацию
   приложения. Не добавлять в образ private key, client secret или user token.

## Локальные данные

Токен и refresh token шифруются перед записью в `github-token.enc`; ключ хранится
отдельным файлом `github-token.key` рядом с volume базы и имеет права `0600` на
POSIX. В SQLite сохраняется только выбранный репозиторий. При отключении зашифрованный
токен удаляется, а ключ остаётся для безопасного повторного подключения. Резервная
копия одной только SQLite не переносит авторизацию GitHub.

Device Flow соблюдает выданный GitHub интервал опроса и `slow_down`. Истекший
пользовательский токен обновляется refresh token без client secret; при отзыве
пользователю предлагается повторное подключение. Код авторизации живёт в памяти
процесса и после перезапуска нужно начать подключение заново.

REST-запросы пока закреплены за версией `2022-11-28`; GitHub указывает срок её
поддержки до 10 марта 2028 года. Перед обновлением версии нужно повторить API-
контрактные тесты и проверить breaking changes.

## Ограничения на текущем этапе

Чтение GitHub доступно только для выбранного репозитория. Реальную запись issue из
расследования нельзя включать, пока синтетические метрики и логи не заменены реальными
источниками: смешение данных привело бы к ложным выводам. Это не завершённая веха 4.

Основание: [Device Flow](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app),
[refresh token](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens),
[регистрация GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app),
[установки и репозитории](https://docs.github.com/en/rest/apps/installations),
[разрешения для issues](https://docs.github.com/en/rest/issues/issues).
[Версии REST API](https://docs.github.com/en/rest/about-the-rest-api/api-versions).
