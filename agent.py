#!/usr/bin/env python3
"""
ИИ-агент-разработчик для Termux — v0.5
Движки: Claude Code (подписка, через proot Ubuntu) и API-модели (Gemini/OpenRouter/Ollama).
Только стандартная библиотека Python.
  Пульт (PWA): http://127.0.0.1:8765
  Превью:      http://127.0.0.1:8766/<путь внутри workspace>/
Код живёт в ~/agent-app (git), данные — в ~/agent (настройки, журнал, проекты, бэкапы).
"""
import json, os, shlex, shutil, signal, tarfile, threading, subprocess, mimetypes, time
import urllib.request, urllib.error, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

VERSION = "0.5"
PORT = int(os.environ.get("AGENT_PORT", 8765))
PPORT = PORT + 1                      # превью сайтов — отдельный адрес без доступа к пульту
BASE = Path.home() / "agent"
WS, BK = BASE / "workspace", BASE / "backups"
CFG, LOGF, STATEF, HIST = (BASE / n for n in ("config.json", "log.json", "state.json", "history.json"))
for d in (WS, BK):
    d.mkdir(parents=True, exist_ok=True)
WSR = WS.resolve()
APP = Path(__file__).resolve().parent
STATIC = APP / "static"                 # страница пульта, манифест PWA, service worker, иконки
KIT = APP / "kit"                       # правила (AGENTS.md) и инструменты движков: копируются в workspace
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")
SKIP = {"node_modules", ".git", "build", ".gradle", "__pycache__", ".agent", ".apps-repo"}
MAX_READ, MAX_OUT = 20000, 6000

DEFAULT_CFG = {
    "engine": "claude",
    "claude": {"distro": "ubuntu", "bin": "/root/.local/bin/claude", "model": "", "isolated": True,
               "timeout": 1800, "tools": "Bash,Read,Edit,Write,WebFetch,WebSearch"},
    "provider": "gemini",
    "providers": {
        "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "api_key": "", "model": ""},
        "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key": "", "model": ""},
        "ollama": {"base_url": "http://127.0.0.1:11434/v1", "api_key": "ollama", "model": "qwen2.5:3b"},
    },
    "max_steps": 15, "cmd_timeout": 90, "auto_backups": 15,
}


# ---------- файлы состояния ----------
def jload(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def jsave(p, data, private=False):
    tmp = p.with_name(p.name + ".tmp")          # запись через временный файл — не портится при обрыве
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1 if private else None))
    if private:
        os.chmod(tmp, 0o600)
    tmp.replace(p)


def merge(dst, src):
    for k, v in (src if isinstance(src, dict) else {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        else:
            dst[k] = v
    return dst


cfg = merge(json.loads(json.dumps(DEFAULT_CFG)), jload(CFG, {}))


def save_cfg():
    jsave(CFG, cfg, private=True)


def public_cfg():
    return {"version": VERSION, "engine": cfg["engine"], "provider": cfg["provider"],
            "cc_model": cfg["claude"].get("model", ""),
            "providers": {n: {"base_url": p.get("base_url", ""), "model": p.get("model", ""),
                              "has_key": bool(p.get("api_key"))} for n, p in cfg["providers"].items()}}


def update_cfg(d):
    if d.get("engine") in ("claude", "api"):
        cfg["engine"] = d["engine"]
    if d.get("provider"):
        name = str(d["provider"]).strip()
        cfg["provider"] = name
        p = cfg["providers"].setdefault(name, {"base_url": "", "api_key": "", "model": ""})
        for k in ("base_url", "model", "api_key"):
            if d.get(k):
                p[k] = str(d[k]).strip()
    if "cc_model" in d:
        cfg["claude"]["model"] = str(d["cc_model"] or "").strip()
    save_cfg()


LOG = jload(LOGF, [])
LOG = LOG if isinstance(LOG, list) else []
SEQ = LOG[-1]["seq"] if LOG else 0
STATE = jload(STATEF, {})
STATE = STATE if isinstance(STATE, dict) else {}
LOCK, JOB_LOCK = threading.Lock(), threading.Lock()
BUSY = {"on": False, "stop": False, "proc": None}


def log(kind, text="", **kw):
    """Журнал разговора: хранится на диске, браузер забирает новые записи опросом."""
    global SEQ
    with LOCK:
        SEQ += 1
        LOG.append(dict(seq=SEQ, t=kind, text=str(text)[:20000], **kw))
        del LOG[:-300]
        jsave(LOGF, LOG)


def sync_kit():
    """Правила для движков (AGENTS.md) и инструменты (.agent/) — свежие при каждом запуске агента."""
    if not KIT.is_dir():
        return
    try:
        shutil.copy2(KIT / "AGENTS.md", WS / "AGENTS.md")
        if not (WS / "CLAUDE.md").exists():       # Claude Code читает CLAUDE.md, Codex — AGENTS.md
            (WS / "CLAUDE.md").write_text("@AGENTS.md\n\n# Заметки пользователя\n")
        shutil.copytree(KIT / ".agent", WS / ".agent", dirs_exist_ok=True)
        os.chmod(WS / ".agent" / "apk.sh", 0o755)
    except Exception as e:
        print("Не удалось обновить набор агента (kit):", e)


# ---------- инструменты (для API-моделей) ----------
def safe(rel):
    """Не даём выйти за пределы workspace."""
    p = (WSR / (rel or ".")).resolve()
    if p != WSR and WSR not in p.parents:
        raise ValueError(f"путь вне workspace: {rel}")
    return p


def t_list_files(path="."):
    root, out = safe(path), []
    if not root.exists():
        return "папка не существует"
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(WSR)
        if any(part in SKIP for part in rel.parts):
            continue
        out.append(f"{rel}/" if p.is_dir() else f"{rel}  ({p.stat().st_size} Б)")
        if len(out) >= 300:
            out.append("… (список обрезан)")
            break
    return "\n".join(out) or "(пусто)"


def t_read_file(path):
    data = safe(path).read_text(errors="replace")
    return data if len(data) <= MAX_READ else data[:MAX_READ] + f"\n… (обрезано, всего {len(data)} символов)"


def backup_file(p):
    if p.is_file():
        dst = BK / "files" / time.strftime("%Y%m%d-%H%M%S") / p.relative_to(WSR)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)


def t_write_file(path, content):
    p = safe(path)
    backup_file(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"записано {len(content)} символов в {path}"


DENY = ["rm -rf /", "rm -rf ~", "rm -rf $HOME", "mkfs", ":(){", "> /dev/block"]


def t_run_command(command):
    if any(d in command for d in DENY):
        return "ОТКЛОНЕНО: команда выглядит опасной"
    try:
        r = subprocess.run(command, shell=True, cwd=WSR, capture_output=True, text=True,
                           timeout=cfg["cmd_timeout"], stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"ТАЙМАУТ {cfg['cmd_timeout']} с. Серверы не запускай — превью уже работает."
    out = (r.stdout + (f"\n[stderr]\n{r.stderr}" if r.stderr else "")).strip()
    return f"exit={r.returncode}\n" + (out if len(out) <= MAX_OUT else "…" + out[-MAX_OUT:])


def t_make_backup(note=""):
    tag = "".join(c for c in note if c.isalnum() or c in "-_")[:30]
    f = BK / f"{time.strftime('%Y%m%d-%H%M%S')}{'-' + tag if tag else ''}.tar.gz"
    with tarfile.open(f, "w:gz") as tar:
        tar.add(WSR, arcname="workspace",
                filter=lambda ti: None if any(s in Path(ti.name).parts for s in SKIP) else ti)
    return f"бэкап создан: {f.name} ({f.stat().st_size // 1024} КБ)"


def auto_backup():
    """Снимок проектов перед каждой задачей; храним последние N."""
    try:
        t_make_backup("auto")
        for f in sorted(BK.glob("*-auto.tar.gz"))[:-cfg.get("auto_backups", 15)]:
            f.unlink()
    except Exception as e:
        log("sys", f"Авто-бэкап не удался: {e}")


FUNCS = {"list_files": t_list_files, "read_file": t_read_file, "write_file": t_write_file,
         "run_command": t_run_command, "make_backup": t_make_backup}


def tool(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}


S = {"type": "string"}
TOOLS = [
    tool("list_files", "Список файлов в папке workspace (рекурсивно)", {"path": S}, []),
    tool("read_file", "Прочитать текстовый файл", {"path": S}, ["path"]),
    tool("write_file", "Создать или полностью перезаписать файл", {"path": S, "content": S}, ["path", "content"]),
    tool("run_command", "Выполнить bash-команду в workspace (тесты, проверка)", {"command": S}, ["command"]),
    tool("make_backup", "Сохранить архив всего workspace", {"note": S}, []),
]


def files_list():
    out = []
    for p in sorted(WSR.rglob("*")):
        rel = p.relative_to(WSR)
        if any(part in SKIP for part in rel.parts):
            continue
        url = f"http://127.0.0.1:{PPORT}/{urllib.parse.quote(str(rel))}"
        if p.is_dir():
            out.append({"name": f"{rel}/", "url": url + "/" if (p / "index.html").exists() else ""})
        else:
            out.append({"name": f"{rel}  ({p.stat().st_size} Б)", "url": url})
        if len(out) >= 300:
            break
    return out


# ---------- движок 1: API-модель ----------
SYSTEM = f"""Ты — ИИ-агент-разработчик в Termux на Android (aarch64, без root).
Рабочая папка (workspace) — корень для всех путей. Сайты — в sites/<имя>/, Android-проекты — в android/<имя>/.
Правила:
- Сначала посмотри файлы (list_files/read_file), потом меняй.
- Пиши файлы целиком через write_file (старая версия сохраняется в бэкап).
- После изменений проверяй результат через run_command: python -m py_compile, node --check, тесты.
  Если ошибка — исправь и проверь снова.
- Не запускай серверы: превью сайтов уже работает по адресу http://127.0.0.1:{PPORT}/<путь>/
- Не устанавливай пакеты без просьбы пользователя.
- Отвечай по-русски, кратко: что сделано, где файлы, ссылка на превью."""


def load_history():
    h = jload(HIST, None)
    if isinstance(h, list) and h and h[0].get("role") == "system":
        h[0]["content"] = SYSTEM
        return h
    return [{"role": "system", "content": SYSTEM}]


HISTORY = load_history()


def call_llm(messages):
    name = cfg["provider"]
    p = cfg["providers"].get(name, {})
    if not p.get("model"):
        raise RuntimeError(f"Не указана модель для «{name}». Открой ⚙ Настройки.")
    if not p.get("api_key") and name != "ollama":
        raise RuntimeError(f"Не указан API-ключ для «{name}». Открой ⚙ Настройки.")
    body = {"model": p["model"], "messages": messages, "tools": TOOLS, "tool_choice": "auto"}
    req = urllib.request.Request(p["base_url"].rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + (p.get("api_key") or "none")})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"API {e.code}: {e.read().decode(errors='replace')[:800]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Нет связи с {p['base_url']}: {e.reason}")


def run_api(msg):
    start = len(HISTORY)
    HISTORY.append({"role": "user", "content": msg})
    try:
        for _ in range(cfg["max_steps"]):
            if BUSY["stop"]:
                raise InterruptedError("⏹ Остановлено")
            m = call_llm(HISTORY)["choices"][0]["message"]
            calls = m.get("tool_calls") or []
            if not calls:
                text = m.get("content") or "(пустой ответ)"
                HISTORY.append({"role": "assistant", "content": text})
                log("bot", text)
                return
            HISTORY.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            for c in calls:
                fn = c["function"]["name"]
                raw = c["function"].get("arguments") or "{}"
                args = {}
                try:
                    args = raw if isinstance(raw, dict) else json.loads(raw)
                    res = FUNCS[fn](**args) if fn in FUNCS else f"нет такого инструмента: {fn}"
                except Exception as e:
                    res = f"ОШИБКА: {type(e).__name__}: {e}"
                log("step", tool=fn, brief=brief(args), args=json.dumps(args, ensure_ascii=False)[:800],
                    result=str(res)[:2000], err=str(res).startswith("ОШИБКА"))
                HISTORY.append({"role": "tool", "tool_call_id": c.get("id", ""), "name": fn, "content": str(res)})
        log("bot", "Достигнут лимит шагов. Напиши «продолжай».")
    except InterruptedError as e:
        del HISTORY[start:]
        log("sys", str(e))
    except Exception as e:
        del HISTORY[start:]
        log("err", str(e))
    finally:
        jsave(HIST, HISTORY)


# ---------- движок 2: Claude Code (подписка) ----------
CC_PROMPT = (f"Ты работаешь на Android-телефоне: Ubuntu (proot) внутри Termux. Текущая папка /workspace — "
             f"папка проектов пользователя. Сайты создавай в sites/<имя>/, Android-проекты — в android/<имя>/. "
             f"Превью уже работает: пользователь открывает сайт по адресу http://127.0.0.1:{PPORT}/sites/<имя>/ — "
             f"давай эту ссылку в ответе. Не запускай серверы и другие долгие фоновые процессы. "
             f"После изменений проверяй результат и исправляй ошибки. "
             f"Подробные правила (в том числе сборка Android-приложений) — в /workspace/AGENTS.md, следуй им. "
             f"Отвечай по-русски кратко: что сделано, где файлы, как проверить.")


def brief(a):
    if isinstance(a, dict):
        for k in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
            if a.get(k):
                return str(a[k]).replace("/workspace/", "")[:120]
    return json.dumps(a, ensure_ascii=False)[:120]


def tool_text(c):
    if isinstance(c, list):
        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
    return str(c or "")[:2000]


def kill_proc(p):
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except Exception:
        pass


def run_claude(msg):
    c = cfg["claude"]
    cmd = ["proot-distro", "login", c["distro"]] + (["--isolated"] if c.get("isolated", True) else [])
    cmd += ["--bind", f"{WSR}:/workspace", "--", "bash", "-c",
            "export BASH_DEFAULT_TIMEOUT_MS=900000 BASH_MAX_TIMEOUT_MS=900000; "
            f'cd /workspace && exec {shlex.quote(c["bin"])} "$@"', "claude",
            "-p", msg, "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits", "--allowedTools", c["tools"],
            "--permission-prompts", "none", "--append-system-prompt", CC_PROMPT]
    if c.get("model"):
        cmd += ["--model", c["model"]]
    if STATE.get("cc_session"):
        cmd += ["--resume", STATE["cc_session"]]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", start_new_session=True)
    BUSY["proc"] = p
    flags = {"timeout": False}

    def on_timeout():
        flags["timeout"] = True
        kill_proc(p)

    timer = threading.Timer(c.get("timeout", 1800), on_timeout)
    timer.start()
    pending, tail, note, done = {}, [], "", False
    try:
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                tail = (tail + [line])[-10:]
                continue
            if not isinstance(ev, dict):
                continue
            t = ev.get("type")
            if t in ("system", "result") and ev.get("session_id"):
                STATE["cc_session"] = ev["session_id"]
                jsave(STATEF, STATE)
            if t == "assistant":
                main = ev.get("parent_tool_use_id") is None
                for b in (ev.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and main and str(b.get("text", "")).strip():
                        if note:
                            log("note", note)
                        note = b["text"].strip()
                    elif b.get("type") == "tool_use":
                        if note:
                            log("note", note)
                            note = ""
                        pending[b.get("id")] = (b.get("name", "?"), b.get("input") or {})
            elif t == "user":
                content = (ev.get("message") or {}).get("content")
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        name, a = pending.pop(b.get("tool_use_id"), ("?", {}))
                        log("step", tool=name, brief=brief(a), args=json.dumps(a, ensure_ascii=False)[:800],
                            result=tool_text(b.get("content")), err=bool(b.get("is_error")))
            elif t == "result":
                done = True
                if ev.get("is_error"):
                    log("err", ev.get("result") or f"Claude Code: {ev.get('subtype', 'ошибка')}")
                else:
                    log("bot", ev.get("result") or note or "(пустой ответ)",
                        meta={"sec": round((ev.get("duration_ms") or 0) / 1000), "turns": ev.get("num_turns")})
                note = ""
        p.wait()
    finally:
        timer.cancel()
        BUSY["proc"] = None
    if not done:
        if BUSY["stop"]:
            log("sys", "⏹ Остановлено")
        elif flags["timeout"]:
            lim = c.get("timeout", 1800)
            log("err", f"Задача шла дольше {lim // 60} мин и была остановлена." if lim >= 60
                else f"Задача шла дольше {lim} с и была остановлена.")
        else:
            log("err", f"Claude Code завершился без ответа (код {p.returncode}).\n" + "\n".join(tail) +
                "\nЕсли ошибка повторяется — нажми 🗑 (новый разговор).")


# ---------- задачи ----------
def start_job(msg):
    BUSY.update(on=True, stop=False, proc=None)
    log("user", msg)
    engine = cfg["engine"]

    def work():
        try:
            auto_backup()
            if BUSY["stop"]:
                log("sys", "⏹ Остановлено")
            else:
                (run_claude if engine == "claude" else run_api)(msg)
        except Exception as e:
            log("err", f"{type(e).__name__}: {e}")
        finally:
            BUSY.update(on=False, proc=None)

    threading.Thread(target=work, daemon=True).start()


# ---------- веб-серверы ----------
def send(h, code, body, ctype="application/json; charset=utf-8", extra=None):
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False)
    if isinstance(body, str):
        body = body.encode()
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(body)


def serve_static(h, name):
    f = (STATIC / name).resolve()
    if STATIC not in f.parents or not f.is_file():
        return send(h, 404, "not found", "text/plain")
    ct = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
    if ct.startswith("text/") or ct in ("application/javascript", "application/manifest+json"):
        ct += "; charset=utf-8"
    send(h, 200, f.read_bytes(), ct, {"Cache-Control": "no-cache"})


class P(BaseHTTPRequestHandler):
    """Превью сайтов из workspace (отдельный порт = отдельный origin)."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urllib.parse.unquote(urllib.parse.urlparse(self.path).path)
        try:
            p = safe(path.lstrip("/"))
        except ValueError:
            return send(self, 403, "forbidden", "text/plain")
        if p.is_dir():
            if not path.endswith("/"):
                return send(self, 301, "", "text/plain", {"Location": urllib.parse.quote(path + "/")})
            p = p / "index.html"
        if not p.is_file():
            return send(self, 404, "not found", "text/plain")
        ct = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ct.startswith("text/") or ct in ("application/javascript", "application/json", "image/svg+xml"):
            ct += "; charset=utf-8"
        send(self, 200, p.read_bytes(), ct, {"Cache-Control": "no-store"})


class H(BaseHTTPRequestHandler):
    """Пульт управления."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        if path == "/":
            return serve_static(self, "index.html")
        if path == "/api/config":
            return send(self, 200, public_cfg())
        if path == "/api/log":
            try:
                after = int(urllib.parse.parse_qs(u.query).get("after", ["0"])[0])
            except ValueError:
                after = 0
            with LOCK:
                entries = [e for e in LOG if e["seq"] > after]
            return send(self, 200, {"entries": entries, "busy": BUSY["on"], "engine": cfg["engine"],
                                   "version": VERSION})
        if path == "/api/files":
            return send(self, 200, {"files": files_list()})
        if path.startswith("/preview/"):
            rest = urllib.parse.quote(path[len("/preview/"):])
            return send(self, 302, "", "text/plain", {"Location": f"http://127.0.0.1:{PPORT}/{rest}"})
        if path.count("/") == 1:              # /sw.js, /manifest.webmanifest, /icon-192.png …
            return serve_static(self, path[1:])
        send(self, 404, "not found", "text/plain")

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return send(self, 403, {"error": "чужой origin"})
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return send(self, 415, {"error": "нужен JSON"})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except Exception:
            return send(self, 400, {"error": "неверный JSON"})
        if self.path == "/api/chat":
            msg = str(data.get("message", "")).strip()
            if not msg:
                return send(self, 400, {"error": "пустое сообщение"})
            with JOB_LOCK:
                if BUSY["on"]:
                    return send(self, 409, {"error": "Агент уже работает над задачей"})
                start_job(msg)
            return send(self, 200, {"ok": True})
        if self.path == "/api/stop":
            BUSY["stop"] = True
            if BUSY.get("proc"):
                kill_proc(BUSY["proc"])
            return send(self, 200, {"ok": True})
        if self.path == "/api/config":
            update_cfg(data)
            return send(self, 200, public_cfg())
        if self.path == "/api/reset":
            if BUSY["on"]:
                return send(self, 409, {"error": "Сначала останови текущую задачу (⏹)"})
            del HISTORY[1:]
            jsave(HIST, HISTORY)
            STATE.pop("cc_session", None)
            jsave(STATEF, STATE)
            with LOCK:
                LOG.clear()
            log("sys", "Новый разговор")
            return send(self, 200, {"ok": True})
        if self.path == "/api/backup":
            try:
                return send(self, 200, {"result": t_make_backup("manual")})
            except Exception as e:
                return send(self, 200, {"error": f"бэкап не удался: {e}"})
        send(self, 404, {"error": "not found"})


if __name__ == "__main__":
    sync_kit()
    if not (STATIC / "index.html").is_file():
        raise SystemExit(f"Не найдена папка {STATIC} — запускай agent.py из папки репозитория ai-agent.")
    if not shutil.which("proot-distro"):
        print("Внимание: proot-distro не найден — движок Claude Code работать не будет.")
    prev = ThreadingHTTPServer(("127.0.0.1", PPORT), P)
    threading.Thread(target=prev.serve_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"Агент v{VERSION} запущен: http://127.0.0.1:{PORT}\nПревью сайтов:     http://127.0.0.1:{PPORT}/\n"
          f"Данные: {BASE}\nОстановить: bash {APP}/stop.sh")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        if BUSY.get("proc"):
            kill_proc(BUSY["proc"])
        print("\nОстановлен.")
