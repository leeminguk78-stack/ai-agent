#!/usr/bin/env bash
# Остановка агента. Использование: bash ~/agent-app/stop.sh
if pkill -f "agent.py"; then
  echo "Агент остановлен"
else
  echo "Агент не был запущен"
fi
