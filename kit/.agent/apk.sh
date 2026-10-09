#!/usr/bin/env bash
# Сборка Android-приложения из /workspace/android/<id>/ в APK через GitHub Actions.
# Использование:  bash /workspace/.agent/apk.sh <id>
# Успех: последняя строка APK_URL=<ссылка>. Ошибка: журнал сборки и код выхода 1.
set -uo pipefail

ID="${1:-}"
WS="${AGENT_WS:-/workspace}"
KIT="$WS/.agent"
SRC="$WS/android/$ID"
REPO=ai-agent-apps
CLONE="$WS/.apps-repo"

fail() { echo "ОШИБКА: $*"; exit 1; }

[[ "$ID" =~ ^[a-z][a-z0-9_]*$ ]] || fail "id «$ID» не подходит: только a-z, 0-9 и _, начинается с буквы (пример: calc, todo_list)"
[ -f "$SRC/www/index.html" ] || fail "нет $SRC/www/index.html — сначала создай приложение"
command -v gh >/dev/null || fail "в Ubuntu нет gh: apt install -y gh"
gh auth status >/dev/null 2>&1 || fail "нет входа в GitHub: gh auth login -h github.com -p https -s workflow -w"

OWNER=$(gh api user --jq .login) || fail "не удалось узнать логин GitHub"
REMOTE="${APPS_REMOTE:-https://github.com/$OWNER/$REPO.git}"
git config --global user.name >/dev/null || git config --global user.name "$OWNER"
git config --global user.email >/dev/null || git config --global user.email "$(gh api user --jq .id)+$OWNER@users.noreply.github.com"

# 1. Репозиторий для приложений (создаётся один раз)
if ! gh repo view "$OWNER/$REPO" >/dev/null 2>&1; then
  echo "Создаю репозиторий $OWNER/$REPO для приложений…"
  gh repo create "$OWNER/$REPO" --public --description "Android-приложения, созданные ИИ-агентом" >/dev/null \
    || fail "не удалось создать репозиторий $OWNER/$REPO"
fi

# 2. Локальная копия — только «зеркало» для отправки: всегда приводим её к состоянию GitHub
if [ ! -d "$CLONE/.git" ]; then
  rm -rf "$CLONE" && mkdir -p "$CLONE" || fail "не удалось создать $CLONE"
  git -C "$CLONE" init -q -b main && git -C "$CLONE" remote add origin "$REMOTE"
fi
cd "$CLONE" || fail "нет доступа к $CLONE"
if git fetch -q origin main 2>/dev/null; then
  git checkout -q -B main origin/main && git reset -q --hard origin/main
fi

# 3. Шаблон оболочки и workflow — всегда свежие из набора агента
rm -rf template && cp -r "$KIT/android-template" template
mkdir -p .github/workflows && cp "$KIT/apps-workflow.yml" .github/workflows/build.yml
[ -f README.md ] || printf '# Приложения ИИ-агента\n\nКаждое приложение — папка `apps/<id>/` (веб-приложение в `www/` и описание `app.json`). APK собирается автоматически в GitHub Actions.\n\nСкачать: `https://github.com/%s/%s/releases/download/<id>/<id>.apk`\n' "$OWNER" "$REPO" > README.md

# 4. Само приложение
mkdir -p "apps/$ID" && rm -rf "apps/$ID/www" && cp -r "$SRC/www" "apps/$ID/www" || fail "не удалось скопировать $SRC/www"
[ -f "$SRC/app.json" ] && cp "$SRC/app.json" "apps/$ID/app.json"

# 5. Отправка на сборку
git add -A
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if git diff --cached --quiet; then
  echo "Изменений нет — запускаю пересборку $ID"
  gh workflow run build.yml -R "$OWNER/$REPO" -f app="$ID" >/dev/null || fail "не удалось запустить сборку"
  SHA=""
else
  git commit -q -m "Сборка $ID" && git push -q -u origin main || fail "не удалось отправить изменения на GitHub (git push)"
  SHA=$(git rev-parse HEAD)
fi

# 6. Ждём запуск сборки
echo "Отправлено в GitHub Actions, жду сборку…"
RUN=""
for _ in $(seq 1 40); do
  if [ -n "$SHA" ]; then
    RUN=$(gh run list -R "$OWNER/$REPO" --workflow build.yml --limit 10 --json databaseId,headSha \
          --jq "map(select(.headSha == \"$SHA\"))[0].databaseId // empty" 2>/dev/null)
  else
    RUN=$(gh run list -R "$OWNER/$REPO" --workflow build.yml --event workflow_dispatch --limit 5 --json databaseId,createdAt \
          --jq "map(select(.createdAt >= \"$START\"))[0].databaseId // empty" 2>/dev/null)
  fi
  [ -n "$RUN" ] && break
  sleep 3
done
[ -n "$RUN" ] || fail "сборка не запустилась — проверь https://github.com/$OWNER/$REPO/actions"

# 7. Ждём результат
T0=$(date +%s)
while :; do
  S=$(gh run view "$RUN" -R "$OWNER/$REPO" --json status,conclusion --jq '.status + " " + (.conclusion // "")' 2>/dev/null || echo "unknown")
  case "$S" in completed*) break ;; esac
  E=$(( $(date +%s) - T0 ))
  [ "$E" -gt 1200 ] && fail "сборка идёт дольше 20 минут: https://github.com/$OWNER/$REPO/actions/runs/$RUN"
  echo "… сборка идёт ($E с)"
  sleep 15
done

if [ "$S" = "completed success" ]; then
  echo "✓ APK готов"
  echo "APK_URL=https://github.com/$OWNER/$REPO/releases/download/$ID/$ID.apk"
else
  echo "✗ Сборка не удалась ($S). Журнал ошибки:"
  gh run view "$RUN" -R "$OWNER/$REPO" --log-failed 2>/dev/null | tail -n 80
  echo "Подробнее: https://github.com/$OWNER/$REPO/actions/runs/$RUN"
  exit 1
fi
