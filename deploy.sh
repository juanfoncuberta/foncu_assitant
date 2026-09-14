#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "WARNING: there are uncommitted changes in the working tree."
    read -p "Continue anyway? [y/N] " answer
    case "$answer" in
        [yY]) ;;
        *) echo "Aborted."; exit 1 ;;
    esac
fi

echo ">>> git pull"
git pull

echo ">>> docker compose build"
docker compose build

echo ">>> docker compose up -d"
docker compose up -d

echo ">>> last 30 log lines"
docker compose logs --tail=30
