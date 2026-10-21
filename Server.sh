#!/bin/sh
# Invoke with: sh Server.sh setup | check | start | stop | logs
set -eu
cd -- "$(dirname -- "$0")"
case "${1:-}" in
  setup)
    mkdir -p secrets
    docker build -t studio-bot:mvp .
    docker run --rm -it --user "$(id -u):$(id -g)" \
      --mount "type=bind,src=$PWD,dst=/setup" \
      -e STUDIO_ROOT=/setup studio-bot:mvp setup
    ;;
  check|start)
    if [ ! -f .env ]; then
      echo 'Нет .env. Выполните sh Server.sh setup или отдельно перенесите рабочий .env.'
      exit 1
    fi
    if [ ! -f secrets/google-service-account.json ]; then
      echo 'Нет ключа Google: сохраните его в secrets/google-service-account.json.'
      exit 1
    fi
    docker compose build
    docker compose run --rm --no-deps studio check
    docker compose run --rm --no-deps studio check-google
    if [ "$1" = start ]; then
      docker compose up -d --force-recreate
      docker compose ps
    fi
    ;;
  stop) docker compose stop ;;
  logs) docker compose logs --tail=50 studio ;;
  *) echo 'Использование: sh Server.sh setup | check | start | stop | logs'; exit 1 ;;
esac
