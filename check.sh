#!/usr/bin/env bash
# Проверка возможностей Termux для ИИ-агента. Использование: bash ~/agent-app/check.sh
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
no()   { printf "  \033[31m✗\033[0m %s\n" "$*"; }
info() { printf "  • %s\n" "$*"; }

echo "== Устройство =="
info "Модель:      $(getprop ro.product.model 2>/dev/null)"
info "Android:     $(getprop ro.build.version.release 2>/dev/null) (SDK $(getprop ro.build.version.sdk 2>/dev/null))"
info "Архитектура: $(uname -m), ядер CPU: $(nproc)"

echo "== Память и диск =="
awk '/MemTotal/{printf "  • ОЗУ всего:    %.1f ГБ\n",$2/1048576} /MemAvailable/{printf "  • ОЗУ свободно: %.1f ГБ\n",$2/1048576}' /proc/meminfo
df -h "$HOME" | awk 'NR==2{print "  • Диск Termux:  свободно "$4" из "$2}'

echo "== Termux =="
info "Версия Termux: ${TERMUX_VERSION:-неизвестно}"
[ -d "$HOME/storage" ] && ok "Доступ к памяти телефона (~/storage)" || no "Нет доступа к памяти — выполни: termux-setup-storage"
command -v termux-wake-lock >/dev/null && ok "termux-wake-lock" || no "termux-wake-lock не найден"

echo "== Инструменты =="
check_tool() { local n=$1; shift; if command -v "$n" >/dev/null 2>&1; then ok "$(printf '%-10s' "$n") $("$@" 2>&1 | head -1)"; else no "$n"; fi; }
check_tool python python --version
check_tool git git --version
check_tool node node --version
check_tool proot-distro proot-distro --version
check_tool ollama ollama --version

echo "== Claude Code (в Ubuntu) =="
if ! command -v proot-distro >/dev/null; then
  no "proot-distro не установлен: pkg install proot-distro"
elif [ ! -d "$PREFIX/var/lib/proot-distro/installed-rootfs/ubuntu" ]; then
  no "Ubuntu не установлена: proot-distro install ubuntu"
elif ! proot-distro login ubuntu -- test -x /root/.local/bin/claude 2>/dev/null; then
  no "Claude Code не установлен в Ubuntu (см. README)"
else
  ok "Claude Code $(proot-distro login ubuntu -- /root/.local/bin/claude --version 2>/dev/null | head -1)"
  if proot-distro login ubuntu -- /root/.local/bin/claude auth status >/dev/null 2>&1; then
    ok "Вход в аккаунт Claude выполнен"
  else
    no "Нет входа: proot-distro login ubuntu, затем claude auth login"
  fi
fi

echo "== Codex (в Ubuntu, необязательно) =="
CX='export PATH=/root/.local/bin:/usr/local/bin:$PATH; codex'
if [ ! -d "$PREFIX/var/lib/proot-distro/installed-rootfs/ubuntu" ]; then
  info "Сначала нужна Ubuntu (см. выше)"
elif ! proot-distro login ubuntu -- bash -c "$CX --version" >/dev/null 2>&1; then
  info "Codex не установлен (второй ИИ, подписка ChatGPT) — см. README"
else
  ok "$(proot-distro login ubuntu -- bash -c "$CX --version" 2>/dev/null | head -1)"
  if proot-distro login ubuntu -- bash -c "$CX login status" >/dev/null 2>&1; then
    ok "Вход в ChatGPT выполнен"
  else
    no "Нет входа: proot-distro login ubuntu, затем /root/.local/bin/codex login"
  fi
fi

echo "== Агент =="
if curl -s -m 2 -o /dev/null http://127.0.0.1:8765/api/config; then
  ok "Агент работает: http://127.0.0.1:8765"
else
  info "Агент не запущен: bash ~/agent-app/start.sh"
fi
curl -s -o /dev/null -m 6 https://github.com && ok "Интернет есть" || no "Нет доступа к интернету"
