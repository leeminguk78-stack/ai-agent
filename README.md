# 🤖 ИИ-агент-разработчик для Android

Свой ИИ-агент на телефоне: создаёт сайты (а дальше и Android-приложения) по описанию, правит файлы, проверяет код, исправляет ошибки и делает резервные копии. Работает в **Termux**, управляется из **приложения-пульта** (PWA) на главном экране.

## Как это устроено

```
Пульт (PWA, браузер)  ──►  agent.py в Termux  ──►  движок:
  127.0.0.1:8765               │                    • Claude Code (подписка Claude) в Ubuntu через proot
                               │                    • API-модель: Gemini / OpenRouter / Ollama (локально)
                               ▼
                   ~/agent/workspace — проекты ──► превью сайтов: 127.0.0.1:8766
```

- **Код** агента — в `~/agent-app` (этот репозиторий), обновляется командой `update.sh`.
- **Данные** — в `~/agent`: настройки и ключи (`config.json`), журнал, проекты (`workspace/`), бэкапы (`backups/`). В репозиторий не попадают.
- Только стандартная библиотека Python — никаких `pip install`.

## Установка с нуля

В Termux (команды вводить по одной):

```bash
pkg update -y && pkg install -y python git curl proot-distro
git clone https://github.com/leeminguk78-stack/ai-agent ~/agent-app
bash ~/agent-app/start.sh
```

Движок **Claude Code** (нужна подписка Claude Pro/Max):

```bash
proot-distro install ubuntu
proot-distro login ubuntu
# дальше — внутри Ubuntu:
echo "nameserver 8.8.8.8" > /etc/resolv.conf
apt update && apt install -y curl python3 nodejs git
curl -fsSL https://claude.ai/install.sh | bash
/root/.local/bin/claude auth login
exit
```

Проверить всё сразу: `bash ~/agent-app/check.sh`

## Каждый день

| Действие | Команда в Termux |
|---|---|
| Запустить агента | `bash ~/agent-app/start.sh` |
| Остановить | `bash ~/agent-app/stop.sh` |
| Обновить с GitHub | `bash ~/agent-app/update.sh` |
| Журнал сервера | `tail -n 50 ~/agent/server.log` |

**Установить пульт как приложение:** открой `http://127.0.0.1:8765` в Chrome → меню ⋮ → «Установить приложение» (или «Добавить на главный экран»). Если агент не запущен, приложение подскажет, что делать.

## Приложение «Агент» для Android

Оболочка пульта: открывается во весь экран, сама запускает агента в Termux, принимает «Поделиться → Агент» из любого приложения. Собирается автоматически в GitHub Actions при каждом изменении `android-app/`.

**Скачать последнюю версию:** https://github.com/leeminguk78-stack/ai-agent/releases/latest/download/agent.apk

Первая настройка (один раз):

1. В Termux разреши запуск команд из других приложений:
   ```bash
   mkdir -p ~/.termux && echo "allow-external-apps = true" >> ~/.termux/termux.properties && termux-reload-settings
   ```
2. Установи APK (Android попросит разрешить установку из браузера).
3. При первом нажатии «Запустить агента» разреши «Запуск команд в среде Termux».

Обновления ставятся поверх старой версии. Если Android откажет в установке («конфликт пакетов»), значит ключ подписи сменился (кэш Actions хранит его, пока сборки идут хотя бы раз в неделю) — удали старое приложение и установи новое.

## Android-приложения по описанию

Попроси агента, например: «Сделай Android-приложение — калькулятор чаевых». Агент (Claude Code):

1. пишет приложение как веб-приложение в `~/agent/workspace/android/<id>/www/` и проверяет его в превью;
2. запускает `/workspace/.agent/apk.sh <id>` — проект уходит в твой репозиторий `ai-agent-apps`, GitHub Actions собирает APK;
3. присылает ссылку вида `https://github.com/leeminguk78-stack/ai-agent-apps/releases/download/<id>/<id>.apk`.

Приложения работают офлайн (файлы внутри APK открываются как `https://app.local/`), обновления ставятся поверх.

Первая настройка (один раз) — доступ к GitHub изнутри Ubuntu:

```bash
proot-distro login ubuntu
apt install -y gh
gh auth login -h github.com -p https -s workflow -w
gh auth setup-git
exit
```

Правила для движков лежат в `~/agent/workspace/AGENTS.md` (обновляется автоматически). Свои правила и заметки — в `~/agent/workspace/CLAUDE.md` под строкой `@AGENTS.md`.

## Особенности Android

- Termux должен оставаться открытым в фоне; `start.sh` включает `termux-wake-lock`.
- Если задачи обрываются с кодом `-9`, Android закрывает процессы: в «Параметрах разработчика» включи пункт об ограничениях дочерних процессов (Disable child process restrictions).
- Claude Code запускается изолированно (`proot-distro --isolated`) и видит только `~/agent/workspace`.
- Превью сайтов работает на отдельном порту 8766, поэтому код созданных сайтов не может управлять агентом.

## Планы

- [x] Пульт в браузере, движки Claude Code и API
- [x] PWA: иконка, полноэкранный режим, подсказка при остановленном агенте
- [x] Android-приложение агента (APK через GitHub Actions)
- [ ] Уведомление, когда задача готова
- [x] Сборка Android-приложений по описанию (проекты агента → APK)
- [ ] Codex (ChatGPT Plus) как второй движок: тандем «автор + ревьюер» и резерв
- [ ] Локальная модель через Ollama
