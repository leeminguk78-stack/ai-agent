#!/usr/bin/env python3
"""
ИИ-агент-разработчик для Termux — v0.7
Движки:
  • Claude Code (подписка Claude) и Codex (подписка ChatGPT) — оба в Ubuntu через proot;
  • «Тандем»: один ИИ пишет, второй проверяет копию файлов, автор исправляет замечания;
  • резерв: кончился лимит у одного ИИ — задачу продолжает другой;
  • API-модели (Gemini / OpenRouter / Ollama).
Только стандартная библиотека Python.
  Пульт (PWA): http://127.0.0.1:8765
  Превью:      http://127.0.0.1:8766/<путь внутри workspace>/
Код живёт в ~/agent-app (git), данные — в ~/agent (настройки, журнал, проекты, бэкапы).
"""
import json, os, re, shlex, shutil, signal, tarfile, threading, subprocess, mimetypes, time
import urllib.request, urllib.error, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

VERSION = "0.7.1"
PORT = int(os.environ.get("AGENT_PORT", 8765))
PPORT = PORT + 1                      # превью сайтов — отдельный адрес без доступа к пульту
BASE = Path.home() / "agent"
WS, BK = BASE / "workspace", BASE / "backups"
REVIEW = BASE / "review"              # копия проектов для ревьюера в тандеме (его изменения выбрасываются)
CFG, LOGF, STATEF, HIST = (BASE / n for n in ("config.json", "log.json", "state.json", "history.json"))
for d in (WS, BK):
    d.mkdir(parents=True, exist_ok=True)
WSR = WS.resolve()
APP = Path(__file__).resolve().parent
STATIC = APP / "static"                 # страница пульта, манифест PWA, service worker, иконки
KIT = APP / "kit"                       # правила (AGENTS.md) и инструменты движков: копируются в workspace
mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")
SKIP = {"node_modules", ".git", "build", ".gradle", "__pycache__", ".agent", ".apps-repo", ".claude", ".codex"}
MAX_READ, MAX_OUT = 20000, 6000

DEFAULT_CFG = {
    "engine": "claude",                 # claude | codex | duo («Тандем») | api
    "claude": {"distro": "ubuntu", "bin": "/root/.local/bin/claude", "model": "", "isolated": True,
               "timeout": 1800, "tools": "Bash,Read,Edit,Write,WebFetch,WebSearch"},
    "codex": {"bin": "codex", "model": "", "effort": "", "timeout": 1800},
    "duo": {"author": "claude", "rounds": 1},
    "fallback": True,                   # у одного ИИ кончился лимит — задачу продолжает другой
    "provider": "gemini",
    "providers": {
        "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "api_key": "", "model": ""},
        "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key": "", "model": ""},
        "ollama": {"base_url": "http://127.0.0.1:11434/v1", "api_key": "ollama", "model": "qwen2.5:3b"},
    },
    "max_steps": 15, "cmd_timeout": 90, "auto_backups": 15,
}

# движки по подписке
SUBS = ("claude", "codex")
NAME = {"claude": "Claude", "codex": "Codex"}
LABEL = {"claude": "Claude Code", "codex": "Codex"}
VIA = {"claude": "подписка Claude", "codex": "подписка ChatGPT"}
EFFORTS = ("", "low", "medium", "high", "xhigh")


def other(e):
    return "codex" if e == "claude" else "claude"


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
            "cx_model": cfg["codex"].get("model", ""), "cx_effort": cfg["codex"].get("effort", ""),
            "duo_author": cfg["duo"].get("author", "claude"), "duo_rounds": cfg["duo"].get("rounds", 1),
            "fallback": bool(cfg.get("fallback", True)),
            "providers": {n: {"base_url": p.get("base_url", ""), "model": p.get("model", ""),
                              "has_key": bool(p.get("api_key"))} for n, p in cfg["providers"].items()}}


def update_cfg(d):
    if d.get("engine") in ("claude", "codex", "duo", "api"):
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
    if "cx_model" in d:
        cfg["codex"]["model"] = str(d["cx_model"] or "").strip()
    if d.get("cx_effort") in EFFORTS:
        cfg["codex"]["effort"] = d["cx_effort"]
    if d.get("duo_author") in SUBS:
        cfg["duo"]["author"] = d["duo_author"]
    if str(d.get("duo_rounds")) in ("1", "2"):
        cfg["duo"]["rounds"] = int(d["duo_rounds"])
    if "fallback" in d:
        cfg["fallback"] = bool(d["fallback"])
    save_cfg()


LOG = jload(LOGF, [])
LOG = LOG if isinstance(LOG, list) else []
SEQ = LOG[-1]["seq"] if LOG else 0
STATE = jload(STATEF, {})
STATE = STATE if isinstance(STATE, dict) else {}
STATE.setdefault("synced", {"claude": SEQ})     # до v0.7 весь разговор вёл Claude
LOCK, JOB_LOCK = threading.Lock(), threading.Lock()
# phase — кто чем занят («🔍 Codex проверяет»), doing — текущее действие («Bash ls»)
BUSY = {"on": False, "stop": False, "proc": None, "started": 0, "phase": "", "doing": ""}


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


# ---------- движок: API-модель ----------
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
                log("bot", text, meta={"engine": f"API · {cfg['provider']}",
                                       "model": cfg["providers"].get(cfg["provider"], {}).get("model", "")})
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


# ---------- движки по подписке: общее ----------
ENGINE_PROMPT = (f"Ты работаешь на Android-телефоне: Ubuntu (proot) внутри Termux. Текущая папка /workspace — "
                 f"папка проектов пользователя. Сайты создавай в sites/<имя>/, Android-проекты — в android/<имя>/. "
                 f"Превью уже работает: пользователь открывает сайт по адресу http://127.0.0.1:{PPORT}/sites/<имя>/ — "
                 f"давай эту ссылку в ответе. Не запускай серверы и другие долгие фоновые процессы. "
                 f"После изменений проверяй результат и исправляй ошибки. "
                 f"Подробные правила (в том числе сборка Android-приложений) — в /workspace/AGENTS.md, следуй им. "
                 f"Отвечай по-русски кратко: что сделано, где файлы, как проверить.")

SETUP = {
    "claude": ("\nУстановка (в Termux): proot-distro login ubuntu → curl -fsSL https://claude.ai/install.sh | bash → "
               "/root/.local/bin/claude auth login → exit"),
    "codex": ("\nУстановка (в Termux): proot-distro login ubuntu → curl -fsSL https://chatgpt.com/codex/install.sh | sh → "
              "/root/.local/bin/codex login --device-auth → exit"),
}
LOGIN = {"claude": "\nВойди заново (в Termux): proot-distro login ubuntu → /root/.local/bin/claude auth login → exit",
         "codex": "\nВойди заново (в Termux): proot-distro login ubuntu → /root/.local/bin/codex login --device-auth → exit"}
LIMIT_RE = re.compile(r"hit your [^.\n]{0,40}limit|usage limit|rate.?limit|limit (reached|exceeded)|quota exceeded|"
                      r"out of credits|spend (cap|limit)|shared budget|too many requests|\b429\b", re.I)
AUTH_RE = re.compile(r"\b401\b|unauthori[sz]ed|not logged in|log ?in again|/login|authenticat|token (has )?expired|"
                     r"invalid.{0,20}(token|api.key)", re.I)
LOST_RE = re.compile(r"no conversation found|session.{0,30}not found", re.I)


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


def clip(s, n=4000):
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "\n… (обрезано)"


def first_line(s):
    return (str(s or "").strip().splitlines() or [""])[0][:300]


def argsafe(s):
    """Текст задачи не должен выглядеть как ключ («-…») или подкоманда («update», «resume») программы."""
    return " " + s if s.startswith("-") or re.fullmatch(r"[a-z][a-z-]*", s) else s


def kill_proc(p):
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except Exception:
        pass


def rootfs():
    """Папка Ubuntu на диске Termux; None — запущены не в Termux (проверить нельзя)."""
    pre = os.environ.get("PREFIX")
    return Path(pre) / "var/lib/proot-distro/installed-rootfs" / cfg["claude"]["distro"] if pre else None


def bins(e):
    b = cfg[e].get("bin") or e
    return [b] if b.startswith("/") else [f"/root/.local/bin/{b}", f"/usr/local/bin/{b}", f"/usr/bin/{b}"]


def installed(e):
    """Установлен ли движок в Ubuntu: True / False; None — проверить нельзя."""
    r = rootfs()
    if r is None:
        return None
    return any(os.path.lexists(r / b.lstrip("/")) for b in bins(e))   # lexists: это ссылки внутри Ubuntu


def logged_in(e):
    r = rootfs()
    if r is None or e != "codex":
        return None
    return (r / "root/.codex/auth.json").exists()


# ---------- лимиты подписок ----------
def mark_limit(e, msg, until=0):
    """Запоминаем, что у движка кончился лимит: до сброса задачи сразу уходят второму ИИ."""
    exact = bool(until) and until > time.time()
    STATE.setdefault("limits", {})[e] = {"until": int(until if exact else time.time() + 20 * 60),  # неизвестно — проверим через 20 мин
                                         "exact": exact, "msg": first_line(msg)}
    jsave(STATEF, STATE)


def limited(e):
    return STATE.get("limits", {}).get(e, {}).get("until", 0) > time.time()


def clear_limit(e):
    if STATE.get("limits", {}).pop(e, None):
        jsave(STATEF, STATE)


def usable(e):
    return installed(e) is not False and not limited(e)


def why(e):
    if installed(e) is False:
        return f"{NAME[e]} не установлен"
    m = STATE.get("limits", {}).get(e, {}).get("msg")
    return f"⛔ {NAME[e]} на лимите" + (f" ({m})" if m else "")


def set_usage(e, wins):
    if wins:
        STATE.setdefault("usage", {})[e] = wins
        jsave(STATEF, STATE)


def usage(e):
    """Расход лимита по окнам: [{"w": "5 ч", "pct": 40, "reset": время}]; окна, которые уже сбросились, не показываем."""
    return [w for w in STATE.get("usage", {}).get(e, []) if (w.get("reset") or 0) > time.time()]


def cc_rate(info, res):
    """Событие rate_limit_event от Claude Code: расход лимита и отказ по лимиту."""
    if not isinstance(info, dict):
        return
    wins, uw = [], info.get("unifiedWindows") if isinstance(info.get("unifiedWindows"), dict) else {}
    for key, name in (("five_hour", "5 ч"), ("seven_day", "неделя")):
        w = uw.get(key)
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            wins.append({"w": name, "pct": round(w["utilization"] * 100), "reset": int(w.get("resetsAt") or 0)})
    if not wins and isinstance(info.get("utilization"), (int, float)):
        name = {"five_hour": "5 ч", "seven_day": "неделя"}.get(info.get("rateLimitType"), "лимит")
        wins.append({"w": name, "pct": round(info["utilization"] * 100), "reset": int(info.get("resetsAt") or 0)})
    set_usage("claude", wins)
    if info.get("status") == "rejected" and not info.get("isUsingOverage"):
        res.update(limit_seen=True, limit_until=int(info.get("resetsAt") or 0))


def codex_rollout(tid):
    """Модель и расход лимитов Codex — из файла его сессии (в --json Codex их не пишет)."""
    r = rootfs()
    if r is None or not tid:
        return {}
    files = sorted((r / "root/.codex/sessions").glob(f"*/*/*/rollout-*{tid}.jsonl"))
    if not files:
        return {}
    try:
        with open(files[-1], "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 262144))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return {}
    out = {}
    for line in lines:
        if '"turn_context"' not in line and '"token_count"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        pl = ev.get("payload") if isinstance(ev, dict) else None
        if not isinstance(pl, dict):
            continue
        if ev.get("type") == "turn_context" and pl.get("model"):
            out["model"] = pl["model"]
        if pl.get("type") == "token_count" and isinstance(pl.get("rate_limits"), dict):
            out["rl"] = pl["rate_limits"]
    return out


def cx_usage(rl):
    wins = []
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float)):
            m = int(w.get("window_minutes") or 0)
            name = {300: "5 ч", 10080: "неделя"}.get(m) or (f"{round(m / 60)} ч" if m < 2880 else f"{round(m / 1440)} дн")
            wins.append({"w": name, "pct": round(w["used_percent"]), "reset": int(w.get("resets_at") or 0)})
    return wins


# ---------- запуск движка в Ubuntu ----------
def new_res(e):
    return {"engine": e, "ok": False, "text": "", "error": "", "limit": False, "limit_until": 0, "limit_seen": False,
            "stopped": False, "timeout": False, "session": None, "model": "", "turns": None, "tail": []}


def launch(prog, script, args, ws, res):
    """Запуск CLI движка в Ubuntu (proot): в /workspace он видит только папку ws."""
    c = cfg["claude"]                       # Ubuntu одна на оба движка
    cmd = ["proot-distro", "login", c["distro"]] + (["--isolated"] if c.get("isolated", True) else [])
    cmd += ["--bind", f"{ws}:/workspace", "--", "bash", "-c", script, prog] + args
    try:
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", start_new_session=True)
    except OSError as e:
        res["error"] = f"Не удалось запустить proot-distro ({e}). В Termux: pkg install proot-distro"
        return None


def events(p, timeout, res):
    """Построчно читаем вывод движка: JSON-события отдаём наружу, прочие строки копим в res["tail"]."""
    BUSY["proc"] = p
    if BUSY["stop"]:                        # ⏹ нажали, пока процесс запускался
        kill_proc(p)

    def on_timeout():
        res["timeout"] = True
        kill_proc(p)

    timer = threading.Timer(timeout, on_timeout)
    timer.start()
    try:
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                res["tail"] = (res["tail"] + [line])[-12:]
                continue
            if isinstance(ev, dict):
                yield ev
        p.wait()
    finally:
        timer.cancel()
        BUSY.update(proc=None, doing="")


def conclude(e, p, res):
    """Итог запуска: остановлен / таймаут / ошибка — и не кончился ли лимит."""
    if res["ok"]:
        return res
    tail = "\n".join(res["tail"])
    if BUSY["stop"]:
        res["stopped"] = True
        return res
    if res["timeout"]:
        lim = cfg[e].get("timeout", 1800)
        res["error"] = (f"Задача шла дольше {lim // 60} мин и была остановлена." if lim >= 60
                        else f"Задача шла дольше {lim} с и была остановлена.")
    elif p is not None and p.returncode == 127 and ("not found" in tail or "No such file" in tail):
        res["error"] = f"{LABEL[e]} не установлен в Ubuntu." + SETUP[e]
    elif not res["error"]:
        res["error"] = f"{LABEL[e]} завершился без ответа (код {p.returncode if p else '?'})." + (f"\n{tail}" if tail else "")
    if res["limit_seen"] or LIMIT_RE.search(res["error"] + "\n" + tail):
        res["limit"] = True
    return res


# ---------- движок: Claude Code ----------
def run_claude(prompt, ws=WSR, sid=None, save=True, tag=""):
    c, res = cfg["claude"], new_res("claude")
    script = ("export BASH_DEFAULT_TIMEOUT_MS=900000 BASH_MAX_TIMEOUT_MS=900000; "
              f'cd /workspace && exec {shlex.quote(c["bin"])} "$@"')
    args = ["-p", argsafe(prompt), "--output-format", "stream-json", "--verbose",
            "--permission-mode", "acceptEdits", "--allowedTools", c["tools"],
            "--permission-prompts", "none", "--append-system-prompt", ENGINE_PROMPT]
    if c.get("model"):
        args += ["--model", c["model"]]
    if sid:
        args += ["--resume", sid]
    p = launch("claude", script, args, ws, res)
    if p is None:
        return res
    kw = {"tag": tag} if tag else {}
    pending, note, init_model = {}, "", ""
    for ev in events(p, c.get("timeout", 1800), res):
        t = ev.get("type")
        if t in ("system", "result") and ev.get("session_id"):
            res["session"] = ev["session_id"]
            if save:
                STATE["cc_session"] = ev["session_id"]
            if t == "system" and ev.get("model"):
                init_model = STATE["cc_model"] = ev["model"]
            jsave(STATEF, STATE)
        if t == "rate_limit_event":
            cc_rate(ev.get("rate_limit_info"), res)
        elif t == "assistant":
            main = ev.get("parent_tool_use_id") is None
            for b in (ev.get("message") or {}).get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and main and str(b.get("text", "")).strip():
                    if note:
                        log("note", note, **kw)
                    note = b["text"].strip()
                elif b.get("type") == "tool_use":
                    if note:
                        log("note", note, **kw)
                        note = ""
                    pending[b.get("id")] = (b.get("name", "?"), b.get("input") or {})
                    BUSY["doing"] = f'{b.get("name", "?")} {brief(b.get("input") or {})}'
        elif t == "user":
            content = (ev.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    name, a = pending.pop(b.get("tool_use_id"), ("?", {}))
                    log("step", tool=name, brief=brief(a), args=json.dumps(a, ensure_ascii=False)[:800],
                        result=tool_text(b.get("content")), err=bool(b.get("is_error")), **kw)
                    BUSY["doing"] = ""
        elif t == "result":
            used = list((ev.get("modelUsage") or {}).keys())
            res["model"] = init_model if (init_model in used or not used) else used[0]
            res["turns"] = ev.get("num_turns")
            if ev.get("is_error"):
                res["error"] = str(ev.get("result") or f"Claude Code: {ev.get('subtype', 'ошибка')}")
            else:
                res.update(ok=True, text=ev.get("result") or note or "(пустой ответ)")
            note = ""
    return conclude("claude", p, res)


# ---------- движок: Codex ----------
def cx_cmd(c):
    """«/bin/bash -lc 'ls -la'» → «ls -la»."""
    m = re.match(r"^\S*?\b(?:ba|z)?sh -l?c (.+)$", c, re.S)
    if m:
        try:
            parts = shlex.split(m.group(1))
            if len(parts) == 1:
                return parts[0]
        except ValueError:
            pass
    return c


def cx_rel(p):
    p = str(p or "")
    return p[len("/workspace/"):] if p.startswith("/workspace/") else p


def cx_doing(it):
    k = it.get("type")
    if k == "command_execution":
        return "Bash " + cx_cmd(str(it.get("command") or "")).replace("/workspace/", "")[:100]
    if k == "file_change":
        return "Edit " + ", ".join(cx_rel(x.get("path")) for x in it.get("changes") or [] if isinstance(x, dict))[:100]
    if k == "web_search":
        return "WebSearch " + str(it.get("query") or "")[:100]
    return ""


def cx_step(it, kw):
    k = it.get("type")
    if k == "command_execution":
        cmd, code, out = cx_cmd(str(it.get("command") or "")), it.get("exit_code"), str(it.get("aggregated_output") or "")
        log("step", tool="Bash", brief=cmd.replace("/workspace/", "")[:120], args=cmd[:800],
            result=(f"exit={code}\n" if code is not None else "") + (out if len(out) <= 2000 else "…" + out[-2000:]),
            err=it.get("status") in ("failed", "declined") or code not in (None, 0), **kw)
    elif k == "file_change":
        ch = [x for x in it.get("changes") or [] if isinstance(x, dict)]
        kinds = {"add": "создан", "update": "изменён", "delete": "удалён"}
        st = it.get("status")
        log("step", tool="Edit", brief=", ".join(cx_rel(x.get("path")) for x in ch)[:120],
            args="\n".join(f'{kinds.get(x.get("kind"), x.get("kind"))}: {cx_rel(x.get("path"))}' for x in ch)[:800],
            result={"completed": "готово", "failed": "не удалось"}.get(st, str(st or "")), err=st == "failed", **kw)
    elif k == "mcp_tool_call":
        log("step", tool=f'{it.get("server", "")}.{it.get("tool", "")}', brief=brief(it.get("arguments") or {}),
            args=json.dumps(it.get("arguments"), ensure_ascii=False)[:800],
            result=str((it.get("error") or {}).get("message") or it.get("result") or "")[:2000],
            err=it.get("status") == "failed", **kw)
    elif k == "web_search":
        q = str(it.get("query") or "")
        log("step", tool="WebSearch", brief=q[:120], args=q[:800], result="", **kw)


def run_codex(prompt, ws=WSR, sid=None, save=True, tag=""):
    c, res = cfg["codex"], new_res("codex")
    script = ('export PATH="/root/.local/bin:/usr/local/bin:$PATH"; '
              f'cd /workspace && exec {shlex.quote(c.get("bin") or "codex")} "$@"')
    # Песочницу Codex не включаем: её роль играет proot --isolated (виден только /workspace)
    args = ["exec", "--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
            "-c", "developer_instructions=" + json.dumps(ENGINE_PROMPT, ensure_ascii=False)]
    if c.get("model"):
        args += ["-m", c["model"]]
    if c.get("effort") in EFFORTS[1:]:
        args += ["-c", f'model_reasoning_effort="{c["effort"]}"']
    args += (["resume", sid] if sid else []) + [argsafe(prompt)]
    p = launch("codex", script, args, ws, res)
    if p is None:
        return res
    kw = {"tag": tag} if tag else {}
    note, errors = "", []
    for ev in events(p, c.get("timeout", 1800), res):
        t, it = ev.get("type"), ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if t == "thread.started" and ev.get("thread_id"):
            res["session"] = ev["thread_id"]
            if save:
                STATE["cx_session"] = ev["thread_id"]
                jsave(STATEF, STATE)
        elif t == "item.started":
            BUSY["doing"] = cx_doing(it) or BUSY["doing"]
        elif t == "item.completed":
            k = it.get("type")
            if k == "agent_message":
                if note:
                    log("note", note, **kw)
                note = str(it.get("text") or "").strip()
            elif k in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                if note:
                    log("note", note, **kw)
                    note = ""
                cx_step(it, kw)
                BUSY["doing"] = ""
            elif k == "error":              # предупреждения Codex — покажем, только если задача упадёт
                res["tail"] = (res["tail"] + [str(it.get("message") or "")])[-12:]
        elif t == "turn.completed":
            res.update(ok=True, text=note or "(пустой ответ)")
            note = ""
        elif t == "turn.failed":
            res["error"] = str((ev.get("error") or {}).get("message") or "Codex: ошибка")
        elif t == "error":                  # бывают и временные («Reconnecting…») — решает итог хода
            errors.append(str(ev.get("message") or ""))
    if not res["ok"] and not res["error"] and errors:
        res["error"] = errors[-1]
    info = codex_rollout(res["session"])
    if info.get("model"):
        res["model"] = STATE["cx_model"] = info["model"]
        jsave(STATEF, STATE)
    if isinstance(info.get("rl"), dict):
        wins = cx_usage(info["rl"])
        set_usage("codex", wins)
        full = [w["reset"] for w in wins if w["pct"] >= 100 and w["reset"]]
        if full:
            res["limit_until"] = max(full)
    return conclude("codex", p, res)


def run_engine(e, prompt, **kw):
    return (run_claude if e == "claude" else run_codex)(prompt, **kw)


# ---------- задачи: один ИИ, резерв, тандем ----------
def skey(e):
    return "cc_session" if e == "claude" else "cx_session"


def cur_model(e):
    seen, chosen = STATE.get("cc_model" if e == "claude" else "cx_model", ""), cfg[e].get("model", "")
    return seen if (not chosen or chosen in seen) else chosen


def with_context(e, msg, upto):
    """Если часть разговора прошла без этого ИИ (работал другой) — коротко пересказываем её."""
    since = STATE.get("synced", {}).get(e, 0)
    with LOCK:
        missed = [x for x in LOG if since < x["seq"] < upto and x["t"] in ("user", "bot")][-6:]
    if not missed:
        return msg
    lines = [("Пользователь" if x["t"] == "user" else (x.get("meta") or {}).get("engine") or "ИИ") + ": " +
             clip(x["text"], 1500) for x in missed]
    return ("[Контекст: эти сообщения разговора прошли без тебя — с пользователем работал другой ИИ. "
            "Файлы в /workspace — в актуальном состоянии.]\n\n" + "\n\n".join(lines) +
            "\n\n[Конец контекста]\n\nНовое сообщение пользователя:\n" + msg)


def handoff(alt, prev, msg, upto):
    return (with_context(alt, msg, upto) +
            f"\n\n[Эту задачу начал {NAME[prev]}, но у него закончился лимит. Часть изменений в /workspace "
            "могла уже быть сделана — проверь текущее состояние файлов и доведи задачу до конца.]")


def author_run(e, prompt, phase):
    """Запуск в основном разговоре движка (продолжая его сессию) с учётом лимитов."""
    BUSY["phase"] = phase
    sid = STATE.get(skey(e))
    r = run_engine(e, prompt, sid=sid)
    if sid and not r["ok"] and not r["stopped"] and LOST_RE.search(r["error"]):
        STATE.pop(skey(e), None)            # сессия потерялась — начинаем новую
        r = run_engine(e, prompt)
    if r["ok"]:
        clear_limit(e)
    elif r["limit"]:
        mark_limit(e, r["error"], r["limit_until"])
    return r


def hint(e, r):
    if r["limit"]:
        alt = other(e)
        if not cfg.get("fallback", True):
            return f"\n\nЛимит {NAME[e]} исчерпан. Включи «Резерв» в ⚙ — тогда задачу продолжит {NAME[alt]}."
        if installed(alt) is False:
            return f"\n\nЛимит {NAME[e]} исчерпан. Подключи {NAME[alt]} (см. ⚙) — он будет подменять."
        return f"\n\nЛимит {NAME[e]} исчерпан, {NAME[alt]} тоже недоступен. Дождись сброса лимита или выбери API-модель."
    if AUTH_RE.search(r["error"] + "\n" + "\n".join(r["tail"])):
        return LOGIN[e]
    if "не установлен" in r["error"]:
        return ""
    return "\nЕсли ошибка повторяется — нажми 🗑 (новый разговор)."


def finish(e, r, review=""):
    """Итог задачи в журнал: ответ, остановка или ошибка с подсказкой."""
    if r["ok"]:
        meta = {"sec": round(time.time() - BUSY["started"]), "engine": LABEL[e], "model": r["model"] or cur_model(e)}
        if r["turns"] is not None and not review:
            meta["turns"] = r["turns"]
        if review:
            meta["review"] = review
        log("bot", r["text"], meta=meta)
        STATE.setdefault("synced", {})[e] = SEQ         # этот ИИ знает разговор до этого места
        jsave(STATEF, STATE)
    elif r["stopped"]:
        log("sys", "⏹ Остановлено")
    else:
        log("err", r["error"] + hint(e, r))


def pick(e):
    """Кого запускать: выбранный ИИ или (при включённом резерве) второй, если выбранный недоступен."""
    alt = other(e)
    if usable(e) or not cfg.get("fallback", True):
        return e
    if usable(alt) or (installed(e) is False and installed(alt) is not False):
        return alt
    return e                                # оба недоступны — пробуем выбранный (лимит мог уже сброситься)


def run_single(e, msg, upto):
    fb = cfg.get("fallback", True)
    if pick(e) != e:
        log("sys", f"{why(e)} — задачу выполнит {NAME[other(e)]}.")
        e = other(e)
    alt = other(e)
    r = author_run(e, with_context(e, msg, upto), "" if e == cfg["engine"] else f"{NAME[e]} (резерв)")
    if r["limit"] and fb and usable(alt):
        log("sys", f"⛔ {NAME[e]}: {first_line(r['error'])}\nЗадачу продолжает {NAME[alt]}.")
        r, e = author_run(alt, handoff(alt, e, msg, upto), f"{NAME[alt]} (резерв)"), alt
    finish(e, r)


def scan():
    """Слепок файлов workspace: путь → (размер, время изменения)."""
    out = {}
    for root, dirs, files in os.walk(WSR):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[os.path.relpath(p, WSR)] = (st.st_size, st.st_mtime_ns)
    return out


def diff(a, b):
    return sorted([k for k in b if a.get(k) != b[k]] + [f"{k} (удалён)" for k in a if k not in b])


def make_review_copy():
    shutil.rmtree(REVIEW, ignore_errors=True)
    shutil.copytree(WSR, REVIEW, symlinks=True, ignore=shutil.ignore_patterns(*SKIP))


def files_md(changed):
    return "\n".join(f"- {x}" for x in changed[:40]) + ("\n- …" if len(changed) > 40 else "")


REVIEW_PROMPT = """Ты — ревьюер. Ты работаешь в паре с другим ИИ: {author} выполнил задачу пользователя, а ты независимо проверяешь результат.
Папка /workspace — КОПИЯ проектов только для проверки: всё, что ты в ней изменишь, будет выброшено. Ничего не исправляй — найди проблемы и опиши их.

Задача пользователя:
<<<
{task}
>>>

Ответ {author}:
<<<
{answer}
>>>

Изменённые файлы:
{files}

Проверь: соответствие задаче; ошибки и баги (запусти проверки: node --check, python3 -m py_compile, тесты — если есть); вёрстку на узком экране телефона и на широком экране раскрытого Fold; для Android-приложений — правила из /workspace/AGENTS.md (работа офлайн, app.json); безопасность.
Не запускай серверы и не собирай APK. Не придирайся к вкусовым мелочам — только то, что действительно стоит исправить.

Ответь по-русски. Первая строка — ровно одна из двух:
ВЕРДИКТ: ОК
ВЕРДИКТ: ЕСТЬ ЗАМЕЧАНИЯ
Если есть замечания — дальше пронумерованный список: что не так, в каком файле, как исправить (кратко)."""

RECHECK_PROMPT = """{author} исправил замечания. Копия файлов в /workspace обновлена.

Ответ {author}:
<<<
{answer}
>>>

Изменённые файлы:
{files}

Проверь снова: исправлены ли прежние замечания и не появилось ли новых серьёзных проблем. Сам ничего не исправляй.
Ответ — в том же формате: первая строка «ВЕРДИКТ: ОК» или «ВЕРДИКТ: ЕСТЬ ЗАМЕЧАНИЯ», затем список."""

FIX_PROMPT = """{reviewer} (независимый ревьюер, проверял копию файлов) прислал замечания к твоей работе:
<<<
{review}
>>>
Исправь то, с чем согласен, и проверь результат. Если с каким-то замечанием не согласен — коротко объясни почему.
Если ты собирал APK — после исправлений собери его заново.
В конце дай пользователю полный итоговый ответ (что сделано, где файлы, ссылки) и одной строкой — что изменилось после проверки."""

VERDICT_RE = re.compile(r"ВЕРДИКТ\W{0,5}(ОК|OK|ЕСТЬ ЗАМЕЧАНИЯ|ЗАМЕЧАНИЯ)", re.I)


def review_ok(t):
    m = VERDICT_RE.search(t or "")
    return bool(m) and m.group(1).upper() in ("ОК", "OK")    # формат не понят — отдаём отзыв автору, он решит


def run_duo(msg, upto):
    """Тандем: автор пишет → ревьюер проверяет копию файлов → автор исправляет (1–2 круга)."""
    a = cfg["duo"].get("author", "claude")
    b, fb = other(a), cfg.get("fallback", True)
    if pick(a) != a:
        log("sys", f"{why(a)} — задачу выполнит {NAME[b]}, без проверки.")
        return finish(b, author_run(b, with_context(b, msg, upto), f"{NAME[b]} (резерв)"))
    before = scan()
    r = author_run(a, with_context(a, msg, upto), f"✍ {NAME[a]} пишет")
    if r["limit"] and fb and usable(b):
        log("sys", f"⛔ {NAME[a]}: {first_line(r['error'])}\nЗадачу продолжает {NAME[b]} (без проверки).")
        return finish(b, author_run(b, handoff(b, a, msg, upto), f"{NAME[b]} (резерв)"))
    if not r["ok"]:
        return finish(a, r)
    changed = diff(before, scan())
    if not changed:
        return finish(a, r, "файлы не менялись — проверка не нужна")
    if not usable(b):
        return finish(a, r, f"без проверки: {why(b)}")
    rounds = 2 if str(cfg["duo"].get("rounds", 1)) == "2" else 1
    rsid, review, fixed = None, "", False
    for i in range(1, rounds + 1):
        if BUSY["stop"]:
            break
        make_review_copy()
        log("sys", f"🔍 {NAME[b]} проверяет " + (f"работу {NAME[a]} (на копии файлов)…" if i == 1 else "исправления…"))
        BUSY["phase"] = f"🔍 {NAME[b]} проверяет" + (f" ({i}/{rounds})" if rounds > 1 else "")
        tmpl = REVIEW_PROMPT if i == 1 else RECHECK_PROMPT
        q = run_engine(b, tmpl.format(author=NAME[a], task=clip(msg), answer=clip(r["text"]), files=files_md(changed)),
                       ws=REVIEW, sid=rsid, save=False, tag="rev")
        if q["stopped"]:
            break
        if not q["ok"]:
            if q["limit"]:
                mark_limit(b, q["error"], q["limit_until"])
            review = f"проверка не удалась: {first_line(q['error'])}"
            log("sys", f"🔍 {NAME[b]}: {review}")
            break
        clear_limit(b)
        rsid, good = q["session"], review_ok(q["text"])
        log("review", q["text"], meta={"engine": LABEL[b], "model": q["model"], "round": i, "ok": good})
        if good:
            review = f"✓ {NAME[b]}: " + ("замечания исправлены" if fixed else "замечаний нет")
            break
        before = scan()
        log("sys", f"✍ {NAME[a]} исправляет замечания…")
        f = author_run(a, FIX_PROMPT.format(reviewer=NAME[b], review=clip(q["text"], 6000)), f"✍ {NAME[a]} исправляет")
        if not f["ok"]:
            if not f["stopped"]:
                log("sys", f"{NAME[a]} не смог внести правки: {first_line(f['error'])}")
                review = f"{NAME[b]} нашёл замечания — не исправлены"
            break
        r, fixed = f, True
        changed = sorted(set(changed) | set(diff(before, scan())))
        review = f"{NAME[b]} нашёл замечания — {NAME[a]} исправил"
    if BUSY["stop"]:
        review = "проверка остановлена"
    finish(a, r, review)
    shutil.rmtree(REVIEW, ignore_errors=True)


def eng_warn(e):
    if installed(e) is False:
        return f"{NAME[e]} не установлен — см. ⚙"
    if logged_in(e) is False:
        return f"нужен вход в {NAME[e]} — см. ⚙"
    return ""


def engine_info():
    """Кто выполняет задачи: движок, модель, способ оплаты, лимиты."""
    e = cfg["engine"]
    if e in SUBS:
        info = {"engine": e, "label": LABEL[e], "via": VIA[e], "model": cur_model(e),
                "model_set": cfg[e].get("model", ""), "warn": eng_warn(e)}
        used = [e]
    elif e == "duo":
        a = cfg["duo"].get("author", "claude")
        info = {"engine": "duo", "label": "Тандем", "via": "подписки Claude + ChatGPT", "author": NAME[a],
                "reviewer": NAME[other(a)], "warn": eng_warn(a) or eng_warn(other(a))}
        used = [a, other(a)]
    else:
        name = cfg["provider"]
        p = cfg["providers"].get(name, {})
        ready = bool(p.get("model")) and (bool(p.get("api_key")) or name == "ollama")
        info = {"engine": "api", "label": f"API · {name}", "via": "локально" if name == "ollama" else "API-ключ",
                "model": p.get("model", ""), "model_set": p.get("model", ""), "warn": "" if ready else "настрой в ⚙"}
        used = []
    lim = STATE.get("limits", {})
    watch = used + ([other(e)] if e in SUBS and cfg.get("fallback", True) else [])   # резерв тоже важен
    info["limits"] = [{"name": NAME[x], "until": lim[x]["until"], "exact": lim[x].get("exact", False)}
                      for x in watch if limited(x)]
    info["high"] = [{"name": NAME[x], "pct": max(w["pct"] for w in usage(x))}
                    for x in used if not limited(x) and usage(x) and max(w["pct"] for w in usage(x)) >= 80]
    info["usage"] = {NAME[x]: usage(x) for x in SUBS if usage(x)}
    return info


def start_job(msg):
    BUSY.update(on=True, stop=False, proc=None, started=time.time(), phase="", doing="")
    log("user", msg)
    upto, mode = SEQ, cfg["engine"]

    def work():
        try:
            auto_backup()
            if BUSY["stop"]:
                log("sys", "⏹ Остановлено")
            elif mode == "api":
                run_api(msg)
            elif mode == "duo":
                run_duo(msg, upto)
            else:
                run_single(mode, msg, upto)
        except Exception as e:
            log("err", f"{type(e).__name__}: {e}")
        finally:
            BUSY.update(on=False, proc=None, phase="", doing="")

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
                                   "version": VERSION, "info": engine_info(), "phase": BUSY["phase"],
                                   "doing": BUSY["doing"],
                                   "elapsed": round(time.time() - BUSY["started"]) if BUSY["on"] else 0})
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
        if not isinstance(data, dict):
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
            for k in ("cc_session", "cx_session"):
                STATE.pop(k, None)
            with LOCK:
                LOG.clear()
            log("sys", "Новый разговор")
            STATE["synced"] = {e: SEQ for e in SUBS}
            jsave(STATEF, STATE)
            return send(self, 200, {"ok": True})
        if self.path == "/api/backup":
            try:
                return send(self, 200, {"result": t_make_backup("manual")})
            except Exception as e:
                return send(self, 200, {"error": f"бэкап не удался: {e}"})
        send(self, 404, {"error": "not found"})


if __name__ == "__main__":
    sync_kit()
    shutil.rmtree(REVIEW, ignore_errors=True)       # копия от прерванной проверки
    if not (STATIC / "index.html").is_file():
        raise SystemExit(f"Не найдена папка {STATIC} — запускай agent.py из папки репозитория ai-agent.")
    if not shutil.which("proot-distro"):
        print("Внимание: proot-distro не найден — движки Claude Code и Codex работать не будут.")
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
