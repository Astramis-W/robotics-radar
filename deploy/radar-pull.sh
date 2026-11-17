#!/usr/bin/env bash
# Подтягивает свежую страницу из репозитория. Ставится в cron на сервере:
#   */15 * * * * /opt/radar/deploy/radar-pull.sh >> /var/log/radar-pull.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --quiet --ff-only
