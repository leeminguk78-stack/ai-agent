#!/usr/bin/env bash
# Запуск агента в фоне. Использование: bash ~/agent-app/start.sh
DIR="$(cd "$(dirname "$0")" && pwd)"
URL="http://127.0.0.1:8765"

if curl -s -m 2 -o /dev/null "$URL/api/config"; then
  echo "Агент уже запущен: $URL"
  exit 0
fi

command -v termux-wake-lock >/dev/null && termux-wake-lock   # не даём Android усыпить Termux
mkdir -p ~/agent
nohup python -u "$DIR/agent.py" > ~/agent/server.log 2>&1 &

for _ in $(seq 1 15); do
  sleep 1
  if curl -s -m 1 -o /dev/null "$URL/api/config"; then
    echo "✓ Агент запущен: $URL"
    exit 0
  fi
done
echo "✗ Агент не запустился. Последние строки журнала:"
tail -n 20 ~/agent/server.log
exit 1
