#!/usr/bin/env bash
# Обновление агента с GitHub и перезапуск. Использование: bash ~/agent-app/update.sh
# Внимание: задача, которая выполняется прямо сейчас, будет остановлена.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

before="$(git rev-parse --short HEAD)"
git pull --ff-only || { echo "✗ Не удалось скачать обновление (git pull)"; exit 1; }
after="$(git rev-parse --short HEAD)"

python -m py_compile agent.py || { echo "✗ В новой версии ошибка — агент не перезапущен"; exit 1; }

if [ "$before" = "$after" ]; then
  echo "Обновлений нет (версия $after)"
else
  echo "Обновлено: $before → $after"
  git log --oneline "$before..$after" | sed 's/^/  • /'
fi

bash "$DIR/stop.sh" >/dev/null 2>&1
sleep 1
bash "$DIR/start.sh"
