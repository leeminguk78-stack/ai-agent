#!/usr/bin/env python3
"""
ИИ-агент-разработчик для Termux — v0.9
Движки:
  • Claude Code (подписка Claude) и Codex (подписка ChatGPT) — оба в Ubuntu через proot;
  • «Тандем»: один ИИ пишет, второй проверяет копию файлов, автор исправляет замечания;
  • «Бесплатный ИИ»: облачная модель по бесплатному ключу (Gemini, OpenRouter);
  • «Локальный ИИ»: модель прямо на телефоне (Ollama) — без интернета и лимитов;
  • резерв: кончился лимит или пропала сеть — задачу продолжает следующий ИИ
    (Claude ⇄ Codex → бесплатный → локальный).
Только стандартная библиотека Python.
  Пульт (PWA): http://127.0.0.1:8765
  Превью:      http://127.0.0.1:8766/<путь внутри workspace>/
Код живёт в ~/agent-app (git), данные — в ~/agent (настройки, журнал, проекты, бэкапы).
"""
import calendar, json, os, queue, re, shlex, shutil, signal, tarfile, threading, subprocess, mimetypes, time
import urllib.request, urllib.error, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

VERSION = "0.9"
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
    "engine": "claude",                 # claude | codex | duo («Тандем») | free («Бесплатный ИИ») | local («Локальный ИИ»)
    "claude": {"distro": "ubuntu", "bin": "/root/.local/bin/claude", "model": "", "isolated": True,
               "timeout": 1800, "tools": "Bash,Read,Edit,Write,WebFetch,WebSearch"},
    "codex": {"bin": "codex", "model": "", "effort": "", "timeout": 1800},
    "duo": {"author": "claude", "rounds": 1},
    "fallback": True,                   # кончился лимит или нет сети — задачу продолжает следующий ИИ
    "free": {"provider": "gemini"},     # «Бесплатный ИИ»: облачный сервис по API-ключу
    "local": {"ctx": 8192},             # «Локальный ИИ» (Ollama): размер контекста модели
    "provider": "gemini",               # (до v0.9) выбранный API-сервис
    "providers": {
        "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "api_key": "", "model": ""},
        "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key": "", "model": ""},
        "ollama": {"base_url": "http://127.0.0.1:11434/v1", "api_key": "ollama", "model": ""},
    },
    "max_steps": 15, "cmd_timeout": 180, "auto_backups": 15,
}

# движки по подписке и на API
SUBS = ("claude", "codex")
APIS = ("free", "local")
ENGINES = SUBS + ("duo",) + APIS
NAME = {"claude": "Claude", "codex": "Codex"}
LABEL = {"claude": "Claude Code", "codex": "Codex"}
VIA = {"claude": "подписка Claude", "codex": "подписка ChatGPT", "free": "бесплатный ключ", "local": "на телефоне"}
PTITLE = {"gemini": "Gemini", "openrouter": "OpenRouter", "ollama": "Ollama"}
DEFAULT_MODEL = {"gemini": "gemini-3.8-flash", "openrouter": "", "ollama": "qwen3:4b"}
OLD_MODELS = {"gemini": ("gemini-2.5-flash", "gemini-2.0-flash"), "ollama": ("qwen2.5:3b",)}   # прежние подсказки
EFFORTS = ("", "low", "medium", "high", "xhigh")
LOCAL_CTX = (4096, 8192, 16384)
LOCAL_MODELS = [("qwen3:4b", "2.5 ГБ — по умолчанию"), ("qwen3:1.7b", "1.4 ГБ — быстрее, проще"),
                ("qwen3.5:4b", "3.4 ГБ — новее и умнее, медленнее"), ("granite4.1:3b", "2.1 ГБ — IBM, код и инструменты"),
                ("llama3.2:3b", "2.0 ГБ")]


def nm(e):
    """Короткое имя движка для сообщений: Claude, Codex, Gemini, «локальный ИИ»."""
    if e in NAME:
        return NAME[e]
    return "локальный ИИ" if e == "local" else PTITLE.get(prov(e), prov(e))


def lb(e):
    """Подпись движка под ответом: «Claude Code», «Бесплатный ИИ», «Локальный ИИ» (модель пишется рядом)."""
    return LABEL.get(e) or ("Локальный ИИ" if e == "local" else "Бесплатный ИИ")


def other(e):
    return "codex" if e == "claude" else "claude"


# ---------- файлы состояния ----------
def jload(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def jsave(p, data, private=False):
    tmp = p.with_name(f"{p.name}.{threading.get_ident()}.tmp")   # через временный файл — не портится при обрыве
    for _ in range(5):                          # данные могут меняться из другого потока прямо во время записи
        try:
            text = json.dumps(data, ensure_ascii=False, indent=1 if private else None)
            break
        except RuntimeError:
            time.sleep(0.01)
    tmp.write_text(text)
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
if cfg["engine"] == "api":                    # до v0.9 был один движок «API-модель»
    cfg["engine"] = "local" if cfg.get("provider") == "ollama" else "free"
if "free" not in jload(CFG, {}) and cfg.get("provider") in ("gemini", "openrouter"):
    cfg["free"]["provider"] = cfg["provider"]
for _n, _old in OLD_MODELS.items():           # прежние модели по умолчанию → нынешние
    if cfg["providers"].get(_n, {}).get("model") in _old:
        cfg["providers"][_n]["model"] = ""
if cfg.get("cmd_timeout") == 90:              # до v0.9: команды теперь идут через Ubuntu (proot) — даём больше времени
    cfg["cmd_timeout"] = 180


def save_cfg():
    jsave(CFG, cfg, private=True)


def public_cfg():
    return {"version": VERSION, "engine": cfg["engine"],
            "cc_model": cfg["claude"].get("model", ""),
            "cx_model": cfg["codex"].get("model", ""), "cx_effort": cfg["codex"].get("effort", ""),
            "duo_author": cfg["duo"].get("author", "claude"), "duo_rounds": cfg["duo"].get("rounds", 1),
            "fallback": bool(cfg.get("fallback", True)),
            "free_provider": cfg["free"].get("provider") or "gemini", "local_ctx": int(cfg["local"].get("ctx") or 8192),
            "default_models": DEFAULT_MODEL, "local_models": LOCAL_MODELS,
            "providers": {n: {"model": p.get("model", ""), "has_key": bool(p.get("api_key"))}
                          for n, p in cfg["providers"].items()}}


def update_cfg(d):
    if d.get("engine") in ENGINES:
        cfg["engine"] = d["engine"]
    was = (cfg["free"].get("provider"), model_of("free"), pconf(prov("free")).get("api_key"))
    if d.get("free_provider") in ("gemini", "openrouter"):
        cfg["free"]["provider"] = d["free_provider"]
    fp = pconf(cfg["free"].get("provider") or "gemini")
    if "free_model" in d:
        fp["model"] = str(d["free_model"] or "").strip()
    if d.get("free_key"):                      # пустое поле — ключ не меняем
        fp["api_key"] = str(d["free_key"]).strip()
    if was != (cfg["free"].get("provider"), model_of("free"), fp.get("api_key")):
        clear_limit("free")                     # у другой модели или ключа — свой лимит
    if "local_model" in d:
        pconf("ollama")["model"] = str(d["local_model"] or "").strip()
    if str(d.get("local_ctx")) in {str(c) for c in LOCAL_CTX}:
        cfg["local"]["ctx"] = int(d["local_ctx"])
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
LOCK, JOB_LOCK, STATE_LOCK = threading.Lock(), threading.Lock(), threading.Lock()


def save_state():
    with STATE_LOCK:                            # состояние пишут задача, опрос лимитов и пульт
        jsave(STATEF, STATE)
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
    """Не даём выйти за пределы workspace. Путь «/workspace/…» (как в Ubuntu) тоже понимаем."""
    rel = re.sub(r"^/?workspace(/|$)", "", str(rel or "").strip()) or "."
    p = (WSR / rel).resolve()
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
    """Команда модели — в той же Ubuntu, что у Claude и Codex, изолированно: видна только папка /workspace."""
    if any(d in command for d in DENY):
        return "ОТКЛОНЕНО: команда выглядит опасной"
    limit = 900 if "apk.sh" in command else int(cfg.get("cmd_timeout") or 180)
    if shutil.which("proot-distro") and rootfs() is not None:
        cmd = proot_cmd("sh", 'export PATH="/root/.local/bin:$PATH"; cd /workspace && eval "$1"', [command])
    else:                                       # Ubuntu нет — прямо в Termux, /workspace = папка проектов
        cmd = ["bash", "-c", re.sub(r"(?<![\w.-])/workspace(?![\w-])", str(WSR), command)]
    try:
        p = subprocess.Popen(cmd, cwd=WSR, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", start_new_session=True)
    except OSError as e:
        return f"ОШИБКА: не удалось запустить команду: {e}"
    BUSY["proc"] = p                            # ⏹ останавливает и команду
    try:
        out, _ = p.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        kill_proc(p)
        out, _ = p.communicate()
        return (f"ТАЙМАУТ {limit} с — команда остановлена. Серверы и фоновые процессы не запускай: превью уже работает.\n"
                + (out or "")[-MAX_OUT:])
    finally:
        BUSY["proc"] = None
    out = (out or "").strip()
    return f"exit={p.returncode}\n" + (out if len(out) <= MAX_OUT else "…" + out[-MAX_OUT:])


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
    tool("run_command", "Выполнить bash-команду в Ubuntu в папке /workspace (проверки, тесты; сборка APK — "
         "bash /workspace/.agent/apk.sh <id>)", {"command": S}, ["command"]),
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


# ---------- движки на API: «Бесплатный ИИ» (Gemini и др.) и «Локальный ИИ» (Ollama на телефоне) ----------
SYSTEM = f"""Ты — ИИ-агент-разработчик на Android-телефоне. Проекты пользователя — в папке /workspace.
Инструменты:
- list_files, read_file, write_file — пути внутри /workspace, например sites/coffee/index.html;
- run_command — bash-команда в Ubuntu в папке /workspace (есть python3, node, git, gh).
Правила:
- Сначала посмотри файлы, потом меняй. Файлы пиши целиком через write_file (старая версия сохраняется в бэкап).
- После изменений проверь: node --check для JS, python3 -m py_compile, тесты. Ошибка — исправь и проверь снова.
- Сайты — в sites/<имя>/. Превью уже работает: http://127.0.0.1:{PPORT}/sites/<имя>/ — дай эту ссылку. Серверы не запускай.
- Не устанавливай пакеты без просьбы пользователя.
- Отвечай по-русски, кратко: что сделано, где файлы, как проверить."""


def system_for(local):
    """Маленькой локальной модели — короткие правила; облачной — ещё и подробные правила папки (AGENTS.md)."""
    if local:
        return SYSTEM
    try:
        rules = (WS / "AGENTS.md").read_text(errors="replace")
    except OSError:
        rules = ""
    return SYSTEM + ("\n\nПодробные правила рабочей папки (AGENTS.md):\n" + rules if rules else "")


def load_history():
    h = jload(HIST, None)
    if isinstance(h, list) and h and h[0].get("role") == "system":
        h[0]["content"] = SYSTEM
        return h
    return [{"role": "system", "content": SYSTEM}]


HISTORY = load_history()                      # общий разговор бесплатного и локального ИИ


class LLMError(Exception):
    """Ошибка API-модели: limit — кончился лимит, offline — нет связи (тогда поможет локальная модель)."""
    def __init__(self, msg, limit=False, offline=False, until=0):
        super().__init__(msg)
        self.limit, self.offline, self.until = limit, offline, until


class Stopped(Exception):
    pass


def prov(e):
    """Сервис API-движка: локальный ИИ — всегда Ollama, бесплатный — выбранный в ⚙ (Gemini по умолчанию)."""
    return "ollama" if e == "local" else cfg["free"].get("provider") or "gemini"


def pconf(name):
    return cfg["providers"].setdefault(name, {"base_url": "", "api_key": "", "model": ""})


def model_of(e):
    return pconf(prov(e)).get("model") or DEFAULT_MODEL.get(prov(e), "")


def interruptible(fn, *a):
    """Долгий запрос к модели можно прервать кнопкой ⏹: ответ, пришедший позже, просто не используем."""
    box = {}

    def run():
        try:
            box["ok"] = fn(*a)
        except BaseException as ex:            # noqa: B902 — переносим любую ошибку в основной поток
            box["err"] = ex

    t = threading.Thread(target=run, daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.5)
        if BUSY["stop"]:
            raise Stopped()
    if "err" in box:
        raise box["err"]
    return box.get("ok")


def pt_midnight(now=None):
    """Следующая полночь по тихоокеанскому времени — тогда Google обнуляет суточные бесплатные лимиты."""
    now = time.time() if now is None else now
    y = time.gmtime(now).tm_year

    def sunday(month, n):                       # n-е воскресенье месяца, около 2:00 по времени США
        wd = time.gmtime(calendar.timegm((y, month, 1, 0, 0, 0))).tm_wday
        return calendar.timegm((y, month, 1 + (6 - wd) % 7 + 7 * (n - 1), 10, 0, 0))

    off = 7 if sunday(3, 2) <= now < sunday(11, 1) else 8      # летом UTC−7, зимой UTC−8
    t = int(now) // 86400 * 86400 + off * 3600
    return t if t > now else t + 86400


def limit_info(body):
    """Ответ 429 → (когда снова можно, суточный ли это лимит).
    Gemini пишет retryDelay и quotaId «…PerDay…», OpenRouter — X-RateLimit-Reset и «per-day»."""
    body = body or ""
    daily = re.search(r"PerDay|per.?day|daily", body, re.I) is not None
    reset = re.search(r'X-RateLimit-Reset"?\s*:\s*"?(\d{10,13})', body)
    delay = re.search(r'"retryDelay"\s*:\s*"(\d+)(?:\.\d+)?s"', body)
    if reset:
        t = int(reset.group(1))
        return (t / 1000 if t > 10 ** 11 else t), daily
    if daily:
        return pt_midnight(), True
    return time.time() + (int(delay.group(1)) if delay else 600), False


BADKEY_RE = re.compile(r"API.?key.{0,20}(not valid|invalid|expired)|API_KEY_INVALID|No auth credentials|User not found",
                       re.I)


def api_err(text):
    """Текст ошибки из ответа сервиса: {"error": {"message": …}} или (у Gemini) [{"error": …}]."""
    try:
        j = json.loads(text)
    except ValueError:
        return text
    j = j[0] if isinstance(j, list) and j else j
    err = j.get("error") if isinstance(j, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or text)
    return str(err or text)


def to_openai(messages):
    """История → запрос OpenAI-формата. Вызовы инструментов уходят обратно как пришли: Gemini 3 требует вернуть
    их подписи (extra_content.google.thought_signature), иначе ответит 400."""
    out = []
    for m in messages:
        m = dict(m)
        if m.get("role") == "tool":
            m.pop("name", None)                 # имя инструмента нужно только Ollama
        elif m.get("role") == "assistant" and m.get("tool_calls") and not m.get("content"):
            m["content"] = None
        out.append(m)
    return out


def call_openai(p, model, messages):
    """OpenAI-совместимый API (Gemini, OpenRouter и др.)."""
    body = {"model": model, "messages": to_openai(messages), "tools": TOOLS, "tool_choice": "auto"}
    url = p["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + (p.get("api_key") or "none")})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")[:8000]
        msg = api_err(text)
        if e.code == 429:
            until, daily = limit_info(text)
            raise LLMError(f"{'Суточный лимит' if daily else 'Лимит запросов'} бесплатного тарифа исчерпан (429): "
                           f"{first_line(msg)}", limit=True, until=until)
        if e.code in (401, 403) or BADKEY_RE.search(text):
            raise LLMError(f"Ключ не подошёл ({e.code}): {first_line(msg).rstrip('.')}. Проверь ключ в ⚙ («Бесплатный ИИ»).")
        raise LLMError(f"API {e.code}: {clip(msg, 800)}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"Нет связи с {url.split('/')[2]}: {getattr(e, 'reason', e)}", offline=True)
    except ValueError:
        raise LLMError(f"{url.split('/')[2]} прислал непонятный ответ (не JSON)")
    if not isinstance(data, dict):
        return {}
    if data.get("error"):                       # OpenRouter иногда присылает ошибку с кодом 200
        err = data["error"]
        msg = str(err.get("message") if isinstance(err, dict) else err)
        if isinstance(err, dict) and err.get("code") == 429:
            until, daily = limit_info(json.dumps(err))
            raise LLMError(f"{'Суточный лимит' if daily else 'Лимит запросов'} бесплатного тарифа исчерпан (429): "
                           f"{first_line(msg)}", limit=True, until=until)
        raise LLMError(f"API: {clip(msg, 800)}")
    return (data.get("choices") or [{}])[0].get("message") or {}


# ---------- Ollama: локальная модель прямо в Termux ----------
OCAPS = {}                                    # что умеет модель: tools, thinking (из /api/show)


def ollama_root():
    return re.sub(r"/v1/?$", "", (pconf("ollama").get("base_url") or "http://127.0.0.1:11434").rstrip("/"))


def ollama_api(path, body=None, timeout=10):
    req = urllib.request.Request(ollama_root() + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ollama_up():
    try:
        ollama_api("/api/version", timeout=2)
        return True
    except Exception:
        return False


def ollama_local():
    return re.match(r"https?://(127\.0\.0\.1|localhost)(:|/|$)", ollama_root()) is not None


def log_tail(path, n=300):
    """Последняя содержательная строка журнала программы (для сообщения об ошибке)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 4000))
            lines = [x.strip() for x in fh.read().decode("utf-8", "replace").splitlines() if x.strip()]
    except OSError:
        return "журнал не найден"
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", lines[-1])[:n] if lines else "журнал пуст"


def ensure_ollama(model):
    """Запустить Ollama (если не запущен) и скачать модель (если её ещё нет) — с прогрессом в пульте."""
    if not ollama_up():
        if not ollama_local():
            raise LLMError(f"Ollama не отвечает по адресу {ollama_root()}", offline=True)
        exe = shutil.which("ollama")
        if not exe:
            raise LLMError("Ollama не установлен. Установи один раз в Termux: pkg install ollama")
        BUSY["doing"] = "запускаю Ollama…"
        port = urllib.parse.urlparse(ollama_root()).port or 11434
        env = dict(os.environ, OLLAMA_HOST=f"127.0.0.1:{port}", OLLAMA_KEEP_ALIVE="30m")
        with open(BASE / "ollama.log", "ab") as lf:
            sp = subprocess.Popen([exe, "serve"], stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT,
                                  env=env, start_new_session=True)     # живёт и после остановки агента
        for _ in range(120):
            if BUSY["stop"]:
                raise Stopped()
            time.sleep(0.5)
            if ollama_up():
                break
            if sp.poll() is not None and not ollama_up():
                raise LLMError(f"Ollama не запустился (код {sp.returncode}): {log_tail(BASE / 'ollama.log')}")
        else:
            raise LLMError("Ollama не запустился за 60 с — подробности в ~/agent/ollama.log")
    have = set()
    for m in ollama_api("/api/tags").get("models") or []:
        have |= {m.get("name"), m.get("model")}
    if model in have or f"{model}:latest" in have:
        return False
    log("sys", f"📥 Скачиваю локальную модель {model} — один раз, это может занять несколько минут…")
    req = urllib.request.Request(ollama_root() + "/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"})
    parts = {}
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            for line in r:
                if BUSY["stop"]:
                    raise Stopped()
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("error"):
                    raise LLMError(f"Не удалось скачать модель {model}: {ev['error']}")
                if ev.get("digest") and ev.get("total"):
                    parts[ev["digest"]] = (ev.get("completed") or 0, ev["total"])
                    done, total = sum(c for c, _ in parts.values()), sum(t for _, t in parts.values())
                    BUSY["doing"] = f"📥 {model}: {done * 100 // total}% из {total / 1e9:.1f} ГБ"
                if ev.get("status") == "success":
                    log("sys", f"✓ Модель {model} скачана")
                    return True
    except urllib.error.HTTPError as e:
        raise LLMError(f"Не удалось скачать модель {model}: {e.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"Не удалось скачать модель {model}: нет связи ({getattr(e, 'reason', e)})", offline=True)
    raise LLMError(f"Скачивание модели {model} оборвалось — попробуй ещё раз")


def ollama_caps(model):
    if model not in OCAPS:
        try:
            OCAPS[model] = set(ollama_api("/api/show", {"model": model}).get("capabilities") or [])
        except Exception:
            OCAPS[model] = {"completion", "tools"}
    return OCAPS[model]


def to_ollama(messages):
    """Разговор в формате OpenAI → родной формат Ollama (аргументы — объектом, ответ инструмента — с tool_name)."""
    out = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "",
                        "tool_calls": [{"type": "function", "function": {
                            "name": c["function"]["name"], "arguments": args_of(c["function"].get("arguments"))}}
                            for c in m["tool_calls"]]})
        elif m.get("role") == "tool":
            out.append({"role": "tool", "content": m.get("content") or "", "tool_name": m.get("name", "")})
        else:
            out.append({"role": m.get("role"), "content": m.get("content") or ""})
    return out


def args_of(raw):
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def call_ollama(model, messages):
    """Родной API Ollama: можно выключить «размышления» (think) и задать размер контекста."""
    caps = ollama_caps(model)
    body = {"model": model, "messages": to_ollama(messages), "stream": False, "keep_alive": "30m",
            "options": {"num_ctx": int(cfg["local"].get("ctx") or 8192)}}
    if "tools" in caps:
        body["tools"] = TOOLS
    if "thinking" in caps:
        body["think"] = False                 # на телефоне размышления слишком долгие
    try:
        data = ollama_api("/api/chat", body, timeout=1800)
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")[:1000]
        try:
            text = json.loads(text).get("error") or text
        except (ValueError, AttributeError):
            pass
        raise LLMError(f"Ollama: {first_line(text)}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"Ollama не отвечает: {getattr(e, 'reason', e)}")
    m = data.get("message") or {}
    calls = [{"id": f"call_{i}", "type": "function",
              "function": {"name": (tc.get("function") or {}).get("name", ""),
                           "arguments": json.dumps((tc.get("function") or {}).get("arguments") or {}, ensure_ascii=False)}}
             for i, tc in enumerate(m.get("tool_calls") or [])]
    return {"role": "assistant", "content": m.get("content") or "", "tool_calls": calls}


THINK_RE = re.compile(r"<think>.*?</think>\s*", re.S)


def salvage_calls(text):
    """Маленькие модели иногда пишут вызов инструмента текстом: {"name": "write_file", "arguments": {...}}."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    try:
        v = json.loads(t)
    except ValueError:
        return []
    out = []
    for i, c in enumerate(v if isinstance(v, list) else [v]):
        if isinstance(c, dict) and c.get("name") in FUNCS:
            a = c.get("arguments", c.get("parameters", {}))
            out.append({"id": f"txt_{i}", "type": "function",
                        "function": {"name": c["name"], "arguments": a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)}})
    return out


def msize(m):
    return len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or "", ensure_ascii=False))


def shrink_call(c):
    """Длинные аргументы старого вызова (содержимое записанного файла) — коротко: файл и так на диске."""
    a = args_of((c.get("function") or {}).get("arguments"))
    a = {k: (clip(v, 200) if isinstance(v, str) else v) for k, v in a.items()}
    return dict(c, function=dict(c.get("function") or {}, arguments=json.dumps(a, ensure_ascii=False)))


def trimmed(h, local):
    """Для маленькой модели — только последние обмены, которые помещаются в её память (целиком, с вопроса)."""
    # ~1.5 символа на токен с запасом: русский текст и код; остаток — правила, инструменты и ответ модели
    budget = int(int(cfg["local"].get("ctx") or 8192) * 1.5) if local else 400000
    rest, start, total = h[1:], len(h) - 1, 0
    for i in range(len(rest) - 1, -1, -1):
        total += msize(rest[i])
        if rest[i].get("role") == "user":
            if total > budget and start < len(rest):
                break
            start = i
    keep = [dict(m) for m in rest[start:]]
    if sum(map(msize, keep)) > budget:            # текущая задача сама не влезает — ужимаем старые выводы
        for m in [m for m in keep if m.get("role") == "tool"][:-2]:
            m["content"] = clip(m["content"], 300)
        if local:                                 # и старые записанные файлы (облачным моделям вызовы не меняем:
            for m in [m for m in keep if m.get("tool_calls")][:-1]:      # Gemini сверяет их подписи)
                m["tool_calls"] = [shrink_call(c) for c in m["tool_calls"]]
    return [{"role": "system", "content": system_for(local)}] + keep


def run_llm(e, prompt):
    """Движки на API: модель вызывает инструменты (файлы, команды) в Termux, пока не даст ответ."""
    res, pname = new_res(e), prov(e)
    p, model, local = pconf(pname), model_of(e), prov(e) == "ollama"
    res["model"] = model
    if not model:
        res["error"] = f"Не указана модель — открой ⚙ («{lb(e)}»)."
        return res
    if not local and not p.get("api_key"):
        res["error"] = f"Нет API-ключа для «{lb(e)}» — открой ⚙ и вставь ключ."
        return res
    start = len(HISTORY)
    try:
        if local:
            interruptible(ensure_ollama, model)
        HISTORY.append({"role": "user", "content": prompt})
        cap = 6000 if local else MAX_READ        # маленькой модели — короткие выдержки из файлов
        for _ in range(cfg["max_steps"]):
            if BUSY["stop"]:
                raise Stopped()
            BUSY["doing"] = "модель думает…"
            msgs = trimmed(HISTORY, local)
            for attempt in range(4):
                try:
                    m = interruptible(call_ollama, model, msgs) if local else interruptible(call_openai, p, model, msgs)
                    break
                except LLMError as ex:            # лимит «в минуту» — ждём и повторяем, «в сутки» — отдаём резерву
                    wait = ex.until - time.time()
                    if not ex.limit or wait > 70 or attempt == 3:
                        raise
                    BUSY["doing"] = f"лимит запросов в минуту — жду {max(1, round(wait))} с…"
                    while time.time() < ex.until:
                        if BUSY["stop"]:
                            raise Stopped()
                        time.sleep(0.5)
            text = THINK_RE.sub("", m.get("content") or "").strip()
            calls = m.get("tool_calls") or salvage_calls(text)
            if not calls:
                HISTORY.append({"role": "assistant", "content": text or "(пустой ответ)"})
                res.update(ok=True, text=text or "(пустой ответ)")
                break
            HISTORY.append({"role": "assistant", "content": "" if salvage_calls(text) else text, "tool_calls": calls})
            for c in calls:
                if BUSY["stop"]:
                    raise Stopped()
                fn = str((c.get("function") or {}).get("name") or "")
                args = args_of((c.get("function") or {}).get("arguments"))
                BUSY["doing"] = f"{fn} {brief(args)}"
                try:
                    out = FUNCS[fn](**args) if fn in FUNCS else f"нет такого инструмента: {fn}"
                except Exception as ex:
                    out = f"ОШИБКА: {type(ex).__name__}: {ex}"
                out = str(out)
                log("step", tool=fn, brief=brief(args), args=json.dumps(args, ensure_ascii=False)[:800],
                    result=out[:2000], err=out.startswith("ОШИБКА"))
                HISTORY.append({"role": "tool", "tool_call_id": c.get("id", ""), "name": fn,
                                "content": out if len(out) <= cap else out[:cap] + "\n… (обрезано)"})
        else:
            res.update(ok=True, text="Достигнут лимит шагов. Напиши «продолжай».")
    except Stopped:
        res["stopped"] = True
    except LLMError as ex:
        res.update(error=str(ex), limit=ex.limit, offline=ex.offline, limit_until=int(ex.until))
    except Exception as ex:
        res["error"] = f"{type(ex).__name__}: {ex}"
    finally:
        BUSY["doing"] = ""
        if not res["ok"]:
            del HISTORY[start:]
        jsave(HIST, HISTORY)
    return res


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
NET_RE = re.compile(r"connection (error|refused|reset)|network (error|is unreachable)|getaddrinfo|ENOTFOUND|EAI_AGAIN|"
                    r"ECONNREFUSED|ETIMEDOUT|name resolution|could not resolve|error sending request|stream disconnected",
                    re.I)


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
    """Папка Ubuntu на диске Termux; None — не нашли (не Termux или незнакомая версия proot-distro): тогда не гадаем."""
    pre = os.environ.get("PREFIX")
    if not pre:
        return None
    base, name = Path(pre) / "var/lib/proot-distro", cfg["claude"]["distro"]
    for p in (base / "containers" / name / "rootfs",      # proot-distro 5+ (новая раскладка)
              base / "installed-rootfs" / name):          # старые версии
        if p.is_dir():
            return p
    return None


def bins(e):
    b = cfg[e].get("bin") or e
    return [b] if b.startswith("/") else [f"/root/.local/bin/{b}", f"/usr/local/bin/{b}", f"/usr/bin/{b}"]


def installed(e):
    """Установлен ли движок: True / False; None — проверить нельзя.
    Бесплатный ИИ — есть ли ключ, локальный — есть ли Ollama."""
    if e == "free":
        return not free_problem()
    if e == "local":
        return bool(shutil.which("ollama")) or ollama_up()
    r = rootfs()
    if r is None:
        return None
    return any(os.path.lexists(r / b.lstrip("/")) for b in bins(e))   # lexists: это ссылки внутри Ubuntu


def free_problem():
    """Чего не хватает бесплатному ИИ: ключа или модели (у OpenRouter модели по умолчанию нет)."""
    if not pconf(prov("free")).get("api_key"):
        return "нет ключа"
    return "" if model_of("free") else "не выбрана модель"


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
    save_state()


def limited(e):
    return STATE.get("limits", {}).get(e, {}).get("until", 0) > time.time()


def clear_limit(e):
    if STATE.get("limits", {}).pop(e, None):
        save_state()


def usable(e):
    return installed(e) is not False and not limited(e)


def why(e):
    if installed(e) is False:
        if e == "free":
            return f"бесплатный ИИ не настроен ({free_problem()})"
        return "Ollama не установлен" if e == "local" else f"{nm(e)} не установлен"
    m = str(STATE.get("limits", {}).get(e, {}).get("msg") or "")
    m = m if len(m) <= 100 else m[:100].rstrip() + "…"
    return f"⛔ {nm(e)} на лимите" + (f" ({m})" if m else "")


def set_usage(e, wins, full=False):
    """Запомнить расход лимита по окнам [{"w": "5 ч", "pct": 40, "reset": время сброса}].
    full=True — полные свежие данные (опрос): по ним же ставим или снимаем отметку «лимит исчерпан»."""
    if not wins:
        return
    old = STATE.setdefault("usage", {}).get(e)
    old = old.get("w", []) if isinstance(old, dict) else (old if isinstance(old, list) else [])
    merged = {w["w"]: w for w in old}
    merged.update({w["w"]: w for w in wins})
    STATE["usage"][e] = {"t": int(time.time()), "w": sorted(merged.values(), key=lambda w: w["w"] != "5 ч")}
    if full:
        full_w = [w for w in wins if w["pct"] >= 100]
        if full_w:
            w = max(full_w, key=lambda x: x.get("reset") or 0)
            mark_limit(e, f"лимит «{w['w']}» исчерпан", w.get("reset") or 0)
        else:
            clear_limit(e)
    save_state()


def usage(e):
    """Расход лимита по окнам; окно, время сброса которого прошло, считаем обнулившимся."""
    u = STATE.get("usage", {}).get(e)
    wins = u.get("w", []) if isinstance(u, dict) else (u if isinstance(u, list) else [])   # список — формат v0.7
    now = time.time()
    return [dict(w, pct=0, reset=0) if 0 < (w.get("reset") or 0) <= now else dict(w) for w in wins]


def usage_time(e):
    u = STATE.get("usage", {}).get(e)
    return u.get("t", 0) if isinstance(u, dict) else 0


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


# ---------- свежий расход лимитов: опрос без траты лимитов ----------
UREF = {"on": False, "t": time.time() - 590, "err": {}, "skip": set()}   # опрос: идёт ли, когда был (первый — через 10 с), ошибки, пропуски
MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
RESET_RE = re.compile(r"(?:([A-Z][a-z]{2}) (\d{1,2}),(?: (\d{4}),)? )?(\d{1,2})(?::(\d{2}))?\s?([ap]m)"
                      r"(?: \(([^)]*)\))?", re.I)
CC_USAGE_RE = re.compile(r"^(Current session|Current week \(all models\)):\s*(\d+)% used(?:\s*·\s*resets (.+))?$", re.M)


def parse_reset(s):
    """«Oct 10, 3:50am (UTC)» → время Unix. Claude Code печатает время в поясе TZ — запускаем его с TZ=UTC."""
    m = RESET_RE.search(s or "")
    if not m:
        return 0
    mon, day, year, hh, mm, ap, tz = m.groups()
    if tz and tz.strip().upper() not in ("UTC", "ETC/UTC", "GMT", "ETC/GMT"):
        return 0                                  # пояс не UTC — не гадаем
    now = time.time()
    g = time.gmtime(now)
    h = int(hh) % 12 + (12 if ap.lower() == "pm" else 0)
    if mon:
        y, mo = int(year) if year else g.tm_year, MONTHS.get(mon.title(), g.tm_mon)
        t = calendar.timegm((y, mo, int(day), h, int(mm or 0), 0))
        if not year and t < now - 86400:          # «Jan 2» в конце декабря — это уже следующий год
            t = calendar.timegm((y + 1, mo, int(day), h, int(mm or 0), 0))
    else:
        t = calendar.timegm((g.tm_year, g.tm_mon, g.tm_mday, h, int(mm or 0), 0))
        if t < now:
            t += 86400
    return t


def probe_claude():
    """Claude Code: `claude -p /usage` — встроенная команда, к модели не обращается."""
    p = subprocess.Popen(proot_cmd("claude", cc_script("export TZ=UTC; "), ["-p", "/usage", "--output-format", "json"]),
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         errors="replace", start_new_session=True)
    try:
        out, err = p.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        kill_proc(p)                            # вся группа: proot, claude и их дети
        p.communicate()
        raise RuntimeError("Claude Code не ответил на /usage за 90 с")
    r = subprocess.CompletedProcess(p.args, p.returncode, out, err)
    text = r.stdout
    for chunk in [r.stdout] + r.stdout.splitlines():
        try:
            ev = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(ev, dict) and isinstance(ev.get("result"), str):
            text = ev["result"]
            break
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    wins = [{"w": "5 ч" if m.group(1) == "Current session" else "неделя", "pct": int(m.group(2)),
             "reset": parse_reset(m.group(3))} for m in CC_USAGE_RE.finditer(text)]
    if not wins:
        # непонятный ответ: сами больше не опрашиваем (вдруг команда ушла модели и тратит лимит) — только по кнопке ⟳
        UREF["skip"].add("claude")
        print(f"[лимиты] ответ Claude Code на /usage (код {r.returncode}): {r.stdout[:1500]!r} stderr: {r.stderr[-500:]!r}",
              flush=True)
        raw = " ".join((text.strip() or r.stderr.strip()).split())
        raise RuntimeError("Claude Code не показал расход лимитов" + (f": «{raw[:200]}»" if raw else ""))
    UREF["skip"].discard("claude")
    set_usage("claude", wins, full=True)


def probe_codex():
    """Codex: `codex app-server` → запрос account/rateLimits/read (так лимиты читают расширения Codex)."""
    p = subprocess.Popen(proot_cmd("codex", cx_script(), ["app-server"]), stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace",
                         start_new_session=True)
    lines = queue.Queue()

    def reader():
        for line in p.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()

    def send(msg):
        p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        p.stdin.flush()

    def answer(i, deadline):
        while True:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                raise RuntimeError("Codex не ответил вовремя")
            if line is None:
                raise RuntimeError("Codex завершился, не сообщив лимиты")
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if isinstance(m, dict) and m.get("id") == i:
                if m.get("error"):
                    err = m["error"]
                    raise RuntimeError(str(err.get("message") if isinstance(err, dict) else err))
                return m.get("result") or {}

    try:
        deadline = time.time() + 60
        send({"method": "initialize", "id": 0,
              "params": {"clientInfo": {"name": "ai_agent", "title": "ИИ-агент (Termux)", "version": VERSION}}})
        answer(0, deadline)
        send({"method": "initialized", "params": {}})
        send({"method": "account/rateLimits/read", "id": 1, "params": {"excludeResetCreditDetails": True}})
        rl = answer(1, deadline).get("rateLimits") or {}
    finally:
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_proc(p)
    wins = cx_usage({k: {"used_percent": w.get("usedPercent"), "window_minutes": w.get("windowDurationMins"),
                         "resets_at": w.get("resetsAt")}
                     for k, w in rl.items() if k in ("primary", "secondary") and isinstance(w, dict)})
    if not wins:
        raise RuntimeError("Codex не сообщил расход лимитов")
    set_usage("codex", wins, full=True)


def refresh_usage(force=False, manual=False):
    """Свежий расход лимитов обеих подписок — в фоне; сам по себе не чаще раза в 10 минут.
    force — сразу (после задачи), manual — по кнопке ⟳ (опрашиваем даже то, что отключили)."""
    if UREF["on"] or (not (force or manual) and time.time() - UREF["t"] < 600):
        return
    UREF["on"] = True

    def work():
        try:
            for e, probe in (("claude", probe_claude), ("codex", probe_codex)):   # по очереди, а не разом
                if installed(e) is False or (not manual and e in UREF["skip"]):
                    continue
                try:
                    probe()
                    UREF["err"].pop(e, None)
                except Exception as ex:
                    UREF["err"][e] = first_line(str(ex)) or type(ex).__name__
                    print(f"[лимиты] {NAME[e]}: {UREF['err'][e]}", flush=True)
        finally:
            UREF.update(on=False, t=time.time())

    threading.Thread(target=work, daemon=True).start()


# ---------- запуск движка в Ubuntu ----------
def new_res(e):
    return {"engine": e, "ok": False, "text": "", "error": "", "limit": False, "limit_until": 0, "limit_seen": False,
            "offline": False, "stopped": False, "timeout": False, "session": None, "model": "", "turns": None,
            "tail": []}


def proot_cmd(prog, script, args, ws=WSR):
    """Команда запуска программы в Ubuntu (proot): в /workspace она видит только папку ws."""
    c = cfg["claude"]                       # Ubuntu одна на оба движка
    cmd = ["proot-distro", "login", c["distro"]] + (["--isolated"] if c.get("isolated", True) else [])
    return cmd + ["--bind", f"{ws}:/workspace", "--", "bash", "-c", script, prog] + args


def cc_script(env=""):
    return (f"{env}export BASH_DEFAULT_TIMEOUT_MS=900000 BASH_MAX_TIMEOUT_MS=900000; "
            f'cd /workspace && exec {shlex.quote(cfg["claude"]["bin"])} "$@"')


def cx_script(env=""):
    return (f'{env}export PATH="/root/.local/bin:/usr/local/bin:$PATH"; '
            f'cd /workspace && exec {shlex.quote(cfg["codex"].get("bin") or "codex")} "$@"')


def launch(prog, script, args, ws, res):
    """Запуск движка в Ubuntu: вывод (stdout+stderr) читаем построчно."""
    cmd = proot_cmd(prog, script, args, ws)
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
    elif NET_RE.search(res["error"] + "\n" + tail):
        res["offline"] = True                   # нет интернета — поможет только локальная модель
    return res


# ---------- движок: Claude Code ----------
def run_claude(prompt, ws=WSR, sid=None, save=True, tag=""):
    c, res = cfg["claude"], new_res("claude")
    script = cc_script()
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
            save_state()
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
    script = cx_script()
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
                save_state()
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
        save_state()
    if isinstance(info.get("rl"), dict):
        wins = cx_usage(info["rl"])
        set_usage("codex", wins)
        full = [w["reset"] for w in wins if w["pct"] >= 100 and w["reset"]]
        if full:
            res["limit_until"] = max(full)
    return conclude("codex", p, res)


def run_engine(e, prompt, **kw):
    if e in APIS:
        return run_llm(e, prompt)
    return (run_claude if e == "claude" else run_codex)(prompt, **kw)


def list_models(name):
    """Модели, доступные по ключу (Gemini, OpenRouter — только бесплатные) или скачанные в Ollama."""
    if name == "ollama":
        st = local_status()
        return st["models"], "" if st["running"] else "Ollama не запущен — агент запустит его сам при первой задаче"
    p = pconf(name)
    if not p.get("api_key"):
        return [], "Сначала вставь ключ и нажми «Сохранить»"
    req = urllib.request.Request(p["base_url"].rstrip("/") + "/models",
                                 headers={"Authorization": "Bearer " + p["api_key"]})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")[:8000]
        if e.code in (401, 403) or BADKEY_RE.search(text):
            return [], f"Ключ не подошёл ({e.code}): {first_line(api_err(text))[:200]}"
        return [], f"Сервис ответил {e.code}: {first_line(api_err(text))[:200]}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return [], f"Нет связи: {getattr(e, 'reason', e)}"
    except ValueError:
        return [], "Сервис прислал непонятный ответ"
    ids = {re.sub(r"^models/", "", str(m.get("id") or "")) for m in (data.get("data") or []) if isinstance(m, dict)}
    if name == "gemini":                        # только текстовые Gemini (без озвучки, картинок, «Live»)
        ids = {i for i in ids if i.startswith("gemini") and
               not re.search(r"tts|live|image|transcribe|embedding|audio|banana|omni|robotics|computer", i)}
    elif name == "openrouter":                  # только бесплатные модели
        ids = {i for i in ids if i.endswith(":free")}
    return sorted(ids - {""}), ""


def local_status():
    up = ollama_up()
    models = []
    if up:
        try:
            models = sorted({m.get("name", "") for m in ollama_api("/api/tags").get("models") or []} - {""})
        except Exception:
            pass
    return {"installed": bool(shutil.which("ollama")) or up, "running": up, "models": models,
            "model": model_of("local"), "url": ollama_root()}


def start_pull(model):
    """Скачать локальную модель заранее (кнопка в ⚙) — как задача: с прогрессом и кнопкой ⏹."""
    BUSY.update(on=True, stop=False, proc=None, started=time.time(), phase="📥 Скачиваю модель", doing="")

    def work():
        try:
            if not interruptible(ensure_ollama, model):
                log("sys", f"✓ Модель {model} уже скачана")
            if cfg["engine"] != "local":
                log("sys", "Чтобы работать с ней, выбери в шапке «Локальный ИИ».")
        except Stopped:
            log("sys", "⏹ Скачивание остановлено")
        except LLMError as ex:
            log("err", str(ex))
        except Exception as ex:
            log("err", f"{type(ex).__name__}: {ex}")
        finally:
            BUSY.update(on=False, proc=None, phase="", doing="")

    threading.Thread(target=work, daemon=True).start()


# ---------- задачи: один ИИ, резерв, тандем ----------
def skey(e):
    return "cc_session" if e == "claude" else "cx_session"


def sync_key(e):
    return "api" if e in APIS else e          # бесплатный и локальный ИИ ведут общий разговор


def cur_model(e):
    if e in APIS:
        return model_of(e)
    seen, chosen = STATE.get("cc_model" if e == "claude" else "cx_model", ""), cfg[e].get("model", "")
    return seen if (not chosen or chosen in seen) else chosen


def with_context(e, msg, upto):
    """Если часть разговора прошла без этого ИИ (работал другой) — коротко пересказываем её."""
    since = STATE.get("synced", {}).get(sync_key(e), 0)
    n, size = (4, 600) if e == "local" else (6, 1500)     # у маленькой локальной модели мало памяти
    with LOCK:
        missed = [x for x in LOG if since < x["seq"] < upto and x["t"] in ("user", "bot")][-n:]
    if not missed:
        return msg
    lines = [("Пользователь" if x["t"] == "user" else (x.get("meta") or {}).get("engine") or "ИИ") + ": " +
             clip(x["text"], size) for x in missed]
    return ("[Контекст: эти сообщения разговора прошли без тебя — с пользователем работал другой ИИ. "
            "Файлы в /workspace — в актуальном состоянии.]\n\n" + "\n\n".join(lines) +
            "\n\n[Конец контекста]\n\nНовое сообщение пользователя:\n" + msg)


def handoff(alt, prev, msg, upto, offline=False):
    return (with_context(alt, msg, upto) +
            f"\n\n[Эту задачу начал {nm(prev)}, но {'пропала связь с интернетом' if offline else 'у него закончился лимит'}. "
            "Часть изменений в /workspace могла уже быть сделана — проверь текущее состояние файлов и доведи задачу до конца.]")


def author_run(e, prompt, phase):
    """Запуск в основном разговоре движка (продолжая его сессию) с учётом лимитов."""
    BUSY["phase"] = phase
    if e in APIS:
        r = run_engine(e, prompt)
    else:
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


def chain(e):
    """Порядок резерва: подписки подменяют друг друга, дальше — бесплатный и локальный ИИ."""
    if e in SUBS:
        return [other(e), "free", "local"]
    return ["local"] if e == "free" else []


def hint(e, r):
    if r["limit"]:
        if not cfg.get("fallback", True):
            return f"\n\nЛимит {nm(e)} исчерпан. Включи «Резерв» в ⚙ — тогда задачу продолжит следующий ИИ."
        if e == "free":
            return ("\n\nПодожди, пока лимит обнулится, или выбери в ⚙ другую модель — у каждой свой лимит "
                    "(например, gemini-3.5-flash-lite). Без лимитов работает локальный ИИ (⚙ → «Локальный ИИ»).")
        return (f"\n\nЛимит {nm(e)} исчерпан, а резервные ИИ недоступны. Дождись сброса лимита или подключи "
                "бесплатный или локальный ИИ (⚙).")
    if r.get("offline"):
        return "" if usable("local") else "\n\nНет связи с интернетом. Без сети работает только локальный ИИ (⚙ → «Локальный ИИ»)."
    if e in APIS:
        return ""
    if AUTH_RE.search(r["error"] + "\n" + "\n".join(r["tail"])):
        return LOGIN[e]
    if "не установлен" in r["error"]:
        return ""
    return "\nЕсли ошибка повторяется — нажми 🗑 (новый разговор)."


def finish(e, r, review=""):
    """Итог задачи в журнал: ответ, остановка или ошибка с подсказкой."""
    if r["ok"]:
        meta = {"sec": round(time.time() - BUSY["started"]), "engine": lb(e), "model": r["model"] or cur_model(e)}
        if r["turns"] is not None and not review:
            meta["turns"] = r["turns"]
        if review:
            meta["review"] = review
        log("bot", r["text"], meta=meta)
        STATE.setdefault("synced", {})[sync_key(e)] = SEQ   # этот ИИ знает разговор до этого места
        save_state()
    elif r["stopped"]:
        log("sys", "⏹ Остановлено")
    else:
        log("err", r["error"] + hint(e, r))


def pick(e):
    """Кого запускать: выбранный ИИ или (при включённом резерве) следующий доступный по цепочке."""
    if usable(e) or not cfg.get("fallback", True):
        return e
    for x in chain(e):
        if usable(x):
            return x
    if installed(e) is False:               # выбранный не установлен — берём следующий установленный (лимит мог сброситься)
        for x in chain(e):
            if installed(x) is not False:
                return x
    return e                                # все недоступны — пробуем выбранный (лимит мог уже сброситься)


def fallback_loop(e, r, msg, upto, tried, note=""):
    """Кончился лимит или пропала сеть — передаём задачу следующему ИИ по цепочке резерва."""
    while cfg.get("fallback", True) and (r["limit"] or r["offline"]) and not r["stopped"]:
        cands = ["local"] if r["offline"] else chain(e)       # без сети выручит только локальная модель
        nxt = next((x for x in cands if x not in tried and usable(x)), None)
        if not nxt:
            break
        log("sys", f"{'📡' if r['offline'] else '⛔'} {nm(e)}: {first_line(r['error'])}\nЗадачу продолжает {nm(nxt)}{note}.")
        r, e = author_run(nxt, handoff(nxt, e, msg, upto, r["offline"]), f"{nm(nxt)} (резерв)"), nxt
        tried.add(nxt)
    return e, r


def run_single(e, msg, upto):
    first = pick(e)
    if first != e:
        log("sys", f"{why(e)} — задачу выполнит {nm(first)}.")
    e = first
    r = author_run(e, with_context(e, msg, upto), "" if e == cfg["engine"] else f"{nm(e)} (резерв)")
    e, r = fallback_loop(e, r, msg, upto, {e})
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
    b = other(a)
    first = pick(a)
    if first != a:
        log("sys", f"{why(a)} — задачу выполнит {nm(first)}, без проверки.")
        r = author_run(first, with_context(first, msg, upto), f"{nm(first)} (резерв)")
        return finish(*fallback_loop(first, r, msg, upto, {a, first}))
    before = scan()
    r = author_run(a, with_context(a, msg, upto), f"✍ {NAME[a]} пишет")
    if r["limit"] or r["offline"]:
        e2, r2 = fallback_loop(a, r, msg, upto, {a}, " (без проверки)")
        if e2 != a:
            return finish(e2, r2)
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
    if e == "free" and installed(e) is False:
        return ("нужен бесплатный ключ" if free_problem() == "нет ключа" else "выбери модель") + " — см. ⚙"
    if e == "local" and installed(e) is False:
        return "установи Ollama — см. ⚙"
    if e in APIS:
        if not limited(e):
            return ""
        spare = cfg.get("fallback", True) and usable("local")
        return f"лимит {nm(e)} исчерпан" + (" — задачи пока выполняет локальный ИИ" if spare else "")
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
    else:                                       # бесплатный или локальный ИИ
        info = {"engine": e, "label": "Бесплатный ИИ" if e == "free" else "Локальный ИИ", "model": model_of(e),
                "via": f"{nm(e)} · бесплатно" if e == "free" else "на телефоне · Ollama", "warn": eng_warn(e)}
        used = []
    lim = STATE.get("limits", {})
    info["usage"] = [{"id": x, "name": NAME[x], "active": x in used, "w": usage(x), "t": usage_time(x),
                      "until": lim[x]["until"] if limited(x) else 0, "exact": bool(lim.get(x, {}).get("exact")),
                      "err": UREF["err"].get(x, "")}
                     for x in SUBS if installed(x) is not False]
    info["usage_busy"] = UREF["on"]
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
            elif mode == "duo":
                run_duo(msg, upto)
            else:
                run_single(mode, msg, upto)
        except Exception as e:
            log("err", f"{type(e).__name__}: {e}")
        finally:
            BUSY.update(on=False, proc=None, phase="", doing="")
            if mode not in APIS:
                refresh_usage(force=True)          # свежий расход лимитов после задачи

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
            if not BUSY["on"]:
                refresh_usage()                     # пульт открыт — раз в 10 минут обновляем лимиты
            return send(self, 200, {"entries": entries, "busy": BUSY["on"], "engine": cfg["engine"],
                                   "version": VERSION, "info": engine_info(), "phase": BUSY["phase"],
                                   "doing": BUSY["doing"],
                                   "elapsed": round(time.time() - BUSY["started"]) if BUSY["on"] else 0})
        if path == "/api/files":
            return send(self, 200, {"files": files_list()})
        if path == "/api/local":
            return send(self, 200, local_status())
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
        if self.path == "/api/usage":
            refresh_usage(manual=True)
            return send(self, 200, {"ok": True})
        if self.path == "/api/models":
            name = str(data.get("provider") or "")
            if name not in ("gemini", "openrouter", "ollama"):
                return send(self, 400, {"error": "неизвестный сервис"})
            models, err = list_models(name)
            return send(self, 200, {"models": models, "error": err})
        if self.path == "/api/pull":
            model = str(data.get("model") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9][\w.\-/]*(:[\w.\-]+)?", model):
                return send(self, 400, {"error": "неверное имя модели"})
            with JOB_LOCK:
                if BUSY["on"]:
                    return send(self, 409, {"error": "Агент занят — дождись конца задачи"})
                start_pull(model)
            return send(self, 200, {"ok": True})
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
            STATE["synced"] = {e: SEQ for e in SUBS + ("api",)}
            save_state()
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
