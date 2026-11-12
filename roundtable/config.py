"""配置。全部来自环境变量或项目根目录下的 .env（见 .env.example），代码里不写死任何部署信息。"""
import ipaddress
import os
import shutil
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"


def load_env() -> None:
    """读 .env。已经在环境里的变量优先（systemd 的 Environment= 可以盖过 .env）。"""
    p = Path(os.environ.get("ROUNDTABLE_ENV_FILE") or ROOT / ".env")
    if not p.is_file():
        return
    vals = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")   # 同一项写了两次，后面的算数
    for k, v in vals.items():
        os.environ.setdefault(k, v)


load_env()


class ConfigError(Exception):
    pass


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _get(name)
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ConfigError(f"{name} 应该是整数，现在是 {raw!r}") from None
    if not lo <= v <= hi:
        raise ConfigError(f"{name} 应该在 {lo}–{hi} 之间，现在是 {v}")
    return v


# ---------- 监听与对外地址 ----------

HOST = _get("ROUNDTABLE_HOST", "127.0.0.1")
PORT = _int("ROUNDTABLE_PORT", 8050, 1, 65535)
# 浏览器里看到的地址（协议 + 域名 + 端口，不带路径）。登录回调、Cookie、跨站检查都以它为准。
PUBLIC_URL = _get("ROUNDTABLE_PUBLIC_URL", f"http://localhost:{PORT}").rstrip("/")
_pu = urllib.parse.urlsplit(PUBLIC_URL)
if _pu.scheme not in ("http", "https") or not _pu.netloc or _pu.path not in ("", "/") or _pu.query:
    raise ConfigError(f"ROUNDTABLE_PUBLIC_URL 应该形如 https://chat.example.com ，现在是 {PUBLIC_URL!r}")
ORIGIN = f"{_pu.scheme}://{_pu.netloc}"
HTTPS = _pu.scheme == "https"
PUBLIC_HOST = (_pu.hostname or "").lower()


def is_loopback_host(host: str) -> bool:
    host = host.strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# ---------- 网页终端（可选，另一个进程，详见 docs/terminal.md） ----------

# 终端在浏览器里的地址。设了它才启用终端；必须和 ROUNDTABLE_PUBLIC_URL 不是同一个来源（另一个子域名，或者另一个端口）。
TERM_URL = _get("ROUNDTABLE_TERM_URL").rstrip("/")
TERM_ORIGIN = ""
if TERM_URL:
    _tu = urllib.parse.urlsplit(TERM_URL)
    if _tu.scheme not in ("http", "https") or not _tu.netloc or _tu.path not in ("", "/") or _tu.query:
        raise ConfigError(f"ROUNDTABLE_TERM_URL 应该形如 https://term.example.com ，现在是 {TERM_URL!r}")
    TERM_ORIGIN = f"{_tu.scheme}://{_tu.netloc}"
    TERM_HOSTNAME = (_tu.hostname or "").lower()
TERM_HOST = _get("ROUNDTABLE_TERM_HOST", "127.0.0.1")
TERM_PORT = _int("ROUNDTABLE_TERM_PORT", 8060, 1, 65535)
# 能进终端的人。留空 = 能登录圆桌的都能进；google 方式下可以只点名其中几个邮箱。
TERM_USERS = frozenset(e.strip().lower() for e in _get("ROUNDTABLE_TERM_USERS").split(",") if e.strip())

# ---------- 数据目录 ----------

DATA = Path(_get("ROUNDTABLE_DATA_DIR") or ROOT / "data").expanduser().resolve()
DB_PATH = DATA / "roundtable.db"
FILES_DIR = DATA / "files"
RUNS_DIR = DATA / "runs"          # codex 每次运行的临时目录
EMPTY_DIR = DATA / "empty"        # claude -p 的工作目录（空）

# ---------- 登录 ----------

AUTH_MODE = _get("ROUNDTABLE_AUTH", "none").lower()       # none | password | google
if AUTH_MODE not in ("none", "password", "google"):
    raise ConfigError(f"ROUNDTABLE_AUTH 只能是 none / password / google，现在是 {AUTH_MODE!r}")
ALLOWED_EMAILS = frozenset(e.strip().lower() for e in _get("ROUNDTABLE_ALLOWED_EMAILS").split(",") if e.strip())
GOOGLE_CLIENT_ID = _get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = _get("GOOGLE_CLIENT_SECRET")
# 默认就是 Google。换成别的 OpenID Connect 提供方理论上可行（要有 email / email_verified），没有逐家测过。
OIDC_ISSUER = _get("ROUNDTABLE_OIDC_ISSUER", "https://accounts.google.com").rstrip("/")
SESSION_DAYS = _int("ROUNDTABLE_SESSION_DAYS", 30, 1, 365)
PASSWORD_MIN_LEN = 12

# ---------- 命令行工具（Claude Code / Codex / CodeBuddy） ----------

HOME = str(Path.home())
CODEX_HOME = _get("CODEX_HOME") or f"{HOME}/.codex"
_SEARCH_PATH = os.pathsep.join([
    f"{HOME}/.local/bin", f"{CODEX_HOME}/packages/standalone/current/bin", f"{HOME}/.npm-global/bin",
    f"{HOME}/.codebuddy/bin",
    "/usr/local/bin", "/opt/homebrew/bin", os.environ.get("PATH", ""), "/usr/bin", "/bin"])


def _find(env: str, name: str) -> str:
    """ROUNDTABLE_CLAUDE_BIN=off 表示不用这个后端；留空就自己在 PATH 里找。"""
    raw = _get(env)
    if raw.lower() in ("off", "none", "0", "false"):
        return ""
    if raw:
        p = Path(raw).expanduser()
        if not (p.is_file() and os.access(p, os.X_OK)):
            raise ConfigError(f"{env}={raw} 不是一个可执行文件")
        return str(p)
    return shutil.which(name, path=_SEARCH_PATH) or ""


CLAUDE_BIN = _find("ROUNDTABLE_CLAUDE_BIN", "claude")
CODEX_BIN = _find("ROUNDTABLE_CODEX_BIN", "codex")
CODEBUDDY_BIN = _find("ROUNDTABLE_CODEBUDDY_BIN", "codebuddy")
# CodeBuddy 抓网页：默认只走它的远程接口。on = 远程接口抓不到时，允许它从这台机器直接去抓（能碰到内网，见 docs/security.md）
CODEBUDDY_LOCAL_FETCH = _get("ROUNDTABLE_CODEBUDDY_LOCAL_FETCH", "off").lower() in ("1", "on", "true", "yes")
CODEX_SANDBOX = _get("ROUNDTABLE_CODEX_SANDBOX", "auto").lower()    # auto | bwrap | off
if CODEX_SANDBOX not in ("auto", "bwrap", "off"):
    raise ConfigError(f"ROUNDTABLE_CODEX_SANDBOX 只能是 auto / bwrap / off，现在是 {CODEX_SANDBOX!r}")
# 默认不让 Codex 读 ~/.codex/config.toml（里面可能配了 MCP 服务器）；on = 读，但 MCP 服务器逐个关掉
CODEX_USER_CONFIG = _get("ROUNDTABLE_CODEX_USER_CONFIG", "off").lower() in ("1", "on", "true", "yes")
# 子进程默认只拿到 HOME / PATH / LANG 和代理设置。要额外传的环境变量在这里点名（例如 ANTHROPIC_API_KEY）。
CLI_PASS_ENV = [n.strip() for n in _get("ROUNDTABLE_CLI_PASS_ENV").split(",") if n.strip()]
_PROXY_ENV = ["HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "all_proxy",
              "no_proxy", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CODEBUDDY_CONFIG_DIR", "CODEBUDDY_INTERNET_ENVIRONMENT"]


def cli_env() -> dict:
    env = {"HOME": HOME, "PATH": _SEARCH_PATH, "LANG": "C.UTF-8"}
    for n in _PROXY_ENV + CLI_PASS_ENV:
        if os.environ.get(n):
            env[n] = os.environ[n]
    return env


# 形如 "opus=Claude Opus,sonnet=Claude Sonnet"；留空用 models.py 里的默认清单
CLAUDE_MODELS = _get("ROUNDTABLE_CLAUDE_MODELS")
# 起标题用哪个模型：auto（有 Claude 就用 haiku）/ off / claude:<id> / or:<OpenRouter 模型 id>
TITLE_MODEL = _get("ROUNDTABLE_TITLE_MODEL", "auto")

# ---------- 上限 ----------

MAX_UPLOAD = _int("ROUNDTABLE_MAX_UPLOAD_MB", 50, 1, 2000) * 1024 * 1024
MAX_TEXT_CHARS = 600_000          # 单个文件抽取文本上限
MAX_IMAGE_SIDE = 2576
MAX_IMAGE_BYTES = 4_500_000
MAX_MODELS_PER_TURN = _int("ROUNDTABLE_MAX_MODELS", 6, 1, 12)
RUN_TIMEOUT = _int("ROUNDTABLE_RUN_TIMEOUT_MIN", 45, 1, 600) * 60      # 单个回答最长时间（秒）
CONCURRENCY = {"claude": _int("ROUNDTABLE_CONCURRENCY_CLAUDE", 4, 1, 32),
               "codex": _int("ROUNDTABLE_CONCURRENCY_CODEX", 4, 1, 32),
               "codebuddy": _int("ROUNDTABLE_CONCURRENCY_CODEBUDDY", 4, 1, 32),
               "or": _int("ROUNDTABLE_CONCURRENCY_OPENROUTER", 8, 1, 64)}


# ---------- MCP（在网页的设置里接；默认一个都没有） ----------

# 本机网关的端口：只听 127.0.0.1，给 claude / codex / codebuddy 子进程用，不要让反向代理转发它。0 = 让系统挑一个空闲的。
MCP_GATEWAY_PORT = _int("ROUNDTABLE_MCP_PORT", 0, 0, 65535)
MCP_CONNECT_TIMEOUT = 20          # 连接、握手、取工具表（秒）
MCP_CALL_TIMEOUT = _int("ROUNDTABLE_MCP_CALL_TIMEOUT", 180, 5, 1800)    # 单次工具调用（秒）
MCP_RESULT_CAP = 40_000           # 一次工具结果最多给模型多少字符
MCP_MAX_CALLS = _int("ROUNDTABLE_MCP_MAX_CALLS", 60, 1, 1000)           # 一个回答里最多调用多少次工具
MCP_MAX_ROUNDS = 16               # OpenRouter：模型和工具最多来回多少轮
MCP_MAX_SERVERS = 20


def openrouter_key() -> str:
    return _get("OPENROUTER_API_KEY")


def check() -> list[str]:
    """启动前的检查。配置不安全就直接拒绝启动（抛 ConfigError），返回的是提醒。"""
    warn = []
    if AUTH_MODE == "none" and not is_loopback_host(HOST):
        raise ConfigError(
            f"ROUNDTABLE_AUTH=none（不要登录）只允许监听本机回环地址，现在 ROUNDTABLE_HOST={HOST}。"
            "要让别的机器能访问，请把 ROUNDTABLE_AUTH 设成 password 或 google。")
    if AUTH_MODE == "google":
        if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
            raise ConfigError("ROUNDTABLE_AUTH=google 需要 GOOGLE_CLIENT_ID 和 GOOGLE_CLIENT_SECRET")
        if not ALLOWED_EMAILS:
            raise ConfigError("ROUNDTABLE_AUTH=google 需要 ROUNDTABLE_ALLOWED_EMAILS（允许登录的邮箱），"
                              "否则任何有 Google 账号的人都能进来")
        iss = urllib.parse.urlsplit(OIDC_ISSUER)
        if iss.scheme != "https" and not (iss.scheme == "http" and is_loopback_host(iss.hostname or "")):
            raise ConfigError("ROUNDTABLE_OIDC_ISSUER 必须是 https 地址")
    if AUTH_MODE != "none" and not HTTPS and not is_loopback_host(PUBLIC_HOST):
        warn.append(f"ROUNDTABLE_PUBLIC_URL={PUBLIC_URL} 不是 https：密码和登录 Cookie 会在网络上明文传输。"
                    "只在可信的内网/VPN 里这样用，放到公网请先配 HTTPS。")
    if AUTH_MODE != "none" and len(ALLOWED_EMAILS) > 1:
        warn.append("ROUNDTABLE_ALLOWED_EMAILS 里有多个邮箱：所有人看到的是同一份对话，用的是同一套订阅/密钥。"
                    "订阅账号一般不允许给别人用，见 docs/backends.md。")
    if TERM_URL:
        if TERM_ORIGIN == ORIGIN:
            raise ConfigError("ROUNDTABLE_TERM_URL 不能和 ROUNDTABLE_PUBLIC_URL 是同一个地址：终端要放在另一个来源下"
                              "（另一个子域名或另一个端口），对话页面万一被注入了脚本才碰不到它。见 docs/terminal.md")
        if (TERM_ORIGIN.startswith("https://")) != HTTPS:
            raise ConfigError("ROUNDTABLE_TERM_URL 和 ROUNDTABLE_PUBLIC_URL 要么都是 https，要么都是 http")
        if AUTH_MODE == "none" and not (is_loopback_host(TERM_HOSTNAME) and is_loopback_host(TERM_HOST)):
            raise ConfigError("ROUNDTABLE_AUTH=none 的时候终端也只能在本机用：ROUNDTABLE_TERM_URL 要是 http://localhost:端口，"
                              "ROUNDTABLE_TERM_HOST 要是回环地址")
        if AUTH_MODE == "google" and TERM_USERS - ALLOWED_EMAILS:
            warn.append("ROUNDTABLE_TERM_USERS 里有不在 ROUNDTABLE_ALLOWED_EMAILS 里的邮箱，这些人登录不了，也就进不了终端")
    return warn
