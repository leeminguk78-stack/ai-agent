#!/usr/bin/env bash
# Остановка агента. Использование: bash ~/agent-app/stop.sh
# Останавливаем ровно процесс агента (по номеру из ~/agent/agent.pid), а не всё, где встречается «agent.py».
PIDF=~/agent/agent.pid
if [ -f "$PIDF" ]; then
  PID=$(cat "$PIDF")
  if [ -r "/proc/$PID/cmdline" ] && tr '\0' ' ' < "/proc/$PID/cmdline" | grep -q "agent\.py"; then
    kill "$PID" && rm -f "$PIDF" && echo "Агент остановлен" && exit 0
  fi
  rm -f "$PIDF"
fi
# запасной путь: агент, запущенный вручную командой python … agent.py
if pkill -f "python[0-9.]* (-u )?[^ ]*agent\.py"; then
  echo "Агент остановлен"
else
  echo "Агент не был запущен"
fi
