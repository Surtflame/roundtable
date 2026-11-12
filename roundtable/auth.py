"""登录。三种模式（ROUNDTABLE_AUTH）：

  none      不登录。只接受本机回环直连（自己电脑上用，或者 SSH 隧道）。
  password  一个密码，没有用户名（这是单人应用）。密码用 `python -m roundtable set-password` 设。
  google    Google 登录（OpenID Connect 授权码流程），只放行 ROUNDTABLE_ALLOWED_EMAILS 里的邮箱。
  external  登录由部署者已有的登录网关负责；每个请求都去问一个仓库外面的小文件（ROUNDTABLE_AUTH_PLUGIN）“这是谁”。
            没有登录页面，也不发会话 Cookie。见本文件末尾和 docs/login.md。

password 和 google 登录成功后发一个随机的会话 Cookie，库里只存它的哈希。
每个请求（页面、静态文件、接口、事件流）都要过 authenticate()，没有例外路径是靠"猜不到"保护的。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
from http.cookies import SimpleCookie

import httpx

from . import config, db

# __Host- 前缀：浏览器只在 HTTPS、Path=/、不带 Domain 时才接受这个 Cookie，子域名没法替你种一个
COOKIE = "__Host-rt_session" if config.HTTPS else "rt_session"
FLOW_COOKIE = "__Host-rt_flow" if config.HTTPS else "rt_flow"
FLOW_TTL = 600
LOCAL_USER = "local"
OWNER_USER = "owner"


class AuthError(Exception):
    """登录失败。code 给登录页挑提示语用，不带任何敏感信息。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- Cookie ----------

def cookie_value(raw: str | None, name: str) -> str | None:
    if not raw:
        return None
    try:
        c = SimpleCookie()
        c.load(raw)
        if name in c:
            return c[name].value
    except Exception:
        pass
    m = re.search(r"(?:^|;\s*)" + re.escape(name) + r"=([^;]+)", raw)
    return m.group(1) if m else None


def set_cookie(name: str, value: str, max_age: int) -> str:
    return (f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
            + ("; Secure" if config.HTTPS else ""))


def clear_cookie(name: str) -> str:
    return set_cookie(name, "", 0)


# ---------- 会话 ----------

def create_session(user: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.x("DELETE FROM sessions WHERE expires_at<?", (now,))
    db.x("INSERT INTO sessions(id,user,created_at,expires_at) VALUES(?,?,?,?)",
         (_hash(token), user, now, now + config.SESSION_DAYS * 86400))
    return token


def session_user(token: str | None) -> str | None:
    if not token or len(token) > 200:
        return None
    r = db.q1("SELECT user, expires_at FROM sessions WHERE id=?", (_hash(token),))
    if not r or r["expires_at"] < time.time():
        return None
    # 换了登录方式、或者把某个邮箱从名单里删了，旧会话立刻作废
    if config.AUTH_MODE == "password" and r["user"] == OWNER_USER:
        return OWNER_USER
    if config.AUTH_MODE == "google" and r["user"] in config.ALLOWED_EMAILS:
        return r["user"]
    return None


def destroy_session(token: str | None) -> None:
    if token:
        db.x("DELETE FROM sessions WHERE id=?", (_hash(token),))


def destroy_all_sessions() -> int:
    n = db.q1("SELECT COUNT(*) AS n FROM sessions")["n"]
    db.x("DELETE FROM sessions")
    return n


# ---------- 每个请求的校验 ----------

_FORWARD_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded", "via")


def _host_is_local(host: str | None) -> bool:
    if not host:
        return False
    name = host.rsplit(":", 1)[0] if not host.endswith("]") else host      # 去掉端口，[::1] 这种保留
    return config.is_loopback_host(name)


def local_request_problem(headers, client) -> str | None:
    """none 模式的检查：必须是本机直连。返回 None 表示没问题。"""
    if not client or not config.is_loopback_host(str(client[0])):
        return "没有开登录（ROUNDTABLE_AUTH=none），只接受本机访问"
    if any(headers.get(h) for h in _FORWARD_HEADERS):
        return ("没有开登录（ROUNDTABLE_AUTH=none）的时候不能放在反向代理后面。"
                "要从别的机器访问，请把 ROUNDTABLE_AUTH 设成 password 或 google")
    if not _host_is_local(headers.get("host")):       # 挡 DNS 重绑定：恶意网页把自己的域名解析到 127.0.0.1
        return "没有开登录（ROUNDTABLE_AUTH=none）的时候只能用 localhost / 127.0.0.1 访问"
    return None


def authenticate(headers, client) -> tuple[str | None, int, str]:
    """→ (user, status, 提示)。user 为 None 就是拒绝。"""
    if config.AUTH_MODE == "none":
        why = local_request_problem(headers, client)
        return (None, 403, why) if why else (LOCAL_USER, 200, "")
    if config.AUTH_MODE == "external":
        return external_user(headers, client, "chat")
    user = session_user(cookie_value(headers.get("cookie"), COOKIE))
    return (user, 200, "") if user else (None, 401, "未登录或登录已过期")


def origin_ok(headers) -> bool:
    """写请求的来源检查。"""
    origin = headers.get("origin")
    if not origin:
        return False
    if origin == config.ORIGIN:
        return True
    if config.AUTH_MODE == "none":
        # 本机模式允许换端口（SSH 隧道常见）：Origin 要和 Host 一致，且 Host 是回环地址
        host = headers.get("host") or ""
        return origin == f"http://{host}" and _host_is_local(host)
    return False


# ---------- 密码 ----------

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1
_FREE_TRIES = 5               # 连续错这么多次以后开始锁
_LOCK_BASE, _LOCK_MAX = 30, 900
_pw_lock = asyncio.Lock()


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)


def _state(key: str, default=None):
    r = db.q1("SELECT value FROM auth_state WHERE key=?", (key,))
    try:
        return json.loads(r["value"]) if r else default
    except ValueError:
        return default


def _set_state(key: str, value) -> None:
    db.x("INSERT INTO auth_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
         (key, json.dumps(value)))


def has_password() -> bool:
    return bool(_state("password"))


def set_password(password: str) -> None:
    """设密码。旧的会话全部作废，失败计数清零。"""
    if len(password) < config.PASSWORD_MIN_LEN:
        raise ValueError(f"密码至少 {config.PASSWORD_MIN_LEN} 位")
    salt = secrets.token_bytes(16)
    h = _scrypt(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    _set_state("password", {"n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
                            "salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(h).decode()})
    _set_state("fail", {"n": 0, "until": 0})
    destroy_all_sessions()


def unlock() -> None:
    _set_state("fail", {"n": 0, "until": 0})


def lock_remaining() -> int:
    return max(0, int((_state("fail", {}) or {}).get("until", 0) - time.time() + 0.999))


async def login_password(password: str) -> str:
    """密码对 → 返回新的会话 token。错了抛 AuthError。

    防暴力破解：不分来源地一起算（单人应用，反向代理后面拿不到可信的对方 IP）。
    连续错 5 次以后每错一次锁一段时间，30 秒起、翻倍、最长 15 分钟；到顶以后一天最多能试 96 次。
    代价是有人在刷登录页的时候你自己也要等——已经登录着的浏览器不受影响。
    """
    async with _pw_lock:
        rec = _state("password")
        if not rec:
            raise AuthError("nopw")
        if lock_remaining():
            raise AuthError("locked")
        ok = False
        if 0 < len(password) <= 1024:
            try:
                got = await asyncio.to_thread(_scrypt, password, base64.b64decode(rec["salt"]),
                                              int(rec["n"]), int(rec["r"]), int(rec["p"]))
                ok = hmac.compare_digest(got, base64.b64decode(rec["hash"]))
            except Exception:
                ok = False
        if ok:
            _set_state("fail", {"n": 0, "until": 0})
            return create_session(OWNER_USER)
        n = int((_state("fail", {}) or {}).get("n", 0)) + 1
        until = 0.0
        if n >= _FREE_TRIES:
            until = time.time() + min(_LOCK_BASE * 2 ** (n - _FREE_TRIES), _LOCK_MAX)
        _set_state("fail", {"n": n, "until": until})
        print(f"auth: 密码错误（连续第 {n} 次）" + (f"，锁定 {int(until - time.time())} 秒" if until else ""),
              flush=True)
        raise AuthError("locked" if until else "badpw")


# ---------- Google / OpenID Connect ----------

_disc: dict = {"at": 0.0, "doc": None}


def _endpoint_ok(url: str) -> bool:
    u = urllib.parse.urlsplit(url or "")
    return u.scheme == "https" or (u.scheme == "http" and config.is_loopback_host(u.hostname or ""))


async def _discovery() -> dict:
    if _disc["doc"] and time.time() - _disc["at"] < 86400:
        return _disc["doc"]
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(config.OIDC_ISSUER + "/.well-known/openid-configuration")
            r.raise_for_status()
            doc = r.json()
    except Exception as e:
        raise AuthError("provider", f"读取登录服务的配置失败：{type(e).__name__}") from None
    if (str(doc.get("issuer", "")).rstrip("/") != config.OIDC_ISSUER
            or not _endpoint_ok(doc.get("authorization_endpoint")) or not _endpoint_ok(doc.get("token_endpoint"))):
        raise AuthError("provider", "登录服务返回的配置不对")
    _disc.update(at=time.time(), doc=doc)
    return doc


def redirect_uri() -> str:
    return config.ORIGIN + "/auth/google/callback"


async def oidc_start() -> tuple[str, str]:
    """→ (要跳去的地址, 放进 Cookie 的 state)。"""
    doc = await _discovery()
    state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    now = time.time()
    db.x("DELETE FROM oidc_pending WHERE created_at<?", (now - FLOW_TTL,))
    db.x("DELETE FROM oidc_pending WHERE state NOT IN (SELECT state FROM oidc_pending ORDER BY created_at DESC LIMIT 200)")
    db.x("INSERT INTO oidc_pending(state,nonce,verifier,created_at) VALUES(?,?,?,?)", (_hash(state), nonce, verifier, now))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = {"client_id": config.GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri(), "response_type": "code",
         "scope": "openid email", "state": state, "nonce": nonce, "code_challenge": challenge,
         "code_challenge_method": "S256", "prompt": "select_account"}
    sep = "&" if "?" in doc["authorization_endpoint"] else "?"
    return doc["authorization_endpoint"] + sep + urllib.parse.urlencode(q), state


def _id_token_claims(tok: str) -> dict:
    try:
        payload = tok.split(".")[1]
        d = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


async def oidc_finish(state: str, cookie_state: str | None, code: str) -> str:
    """回调：校验 state、换 token、核对身份。成功返回新的会话 token。"""
    if not state or not cookie_state or not hmac.compare_digest(state.encode(), cookie_state.encode()):
        raise AuthError("state")          # 不是这个浏览器发起的登录
    row = db.q1("SELECT nonce, verifier, created_at FROM oidc_pending WHERE state=?", (_hash(state),))
    db.x("DELETE FROM oidc_pending WHERE state=?", (_hash(state),))          # 一次性
    if not row or row["created_at"] < time.time() - FLOW_TTL:
        raise AuthError("state")
    if not code or len(code) > 4096:
        raise AuthError("state")
    doc = await _discovery()
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(doc["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
                "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
                "code_verifier": row["verifier"]})
            body = r.json()
    except Exception as e:
        raise AuthError("provider", f"换取令牌失败：{type(e).__name__}") from None
    if r.status_code != 200 or not isinstance(body, dict) or not body.get("id_token"):
        err = body.get("error") if isinstance(body, dict) else ""
        print(f"auth: token endpoint {r.status_code} {err}", flush=True)
        raise AuthError("provider", f"登录服务拒绝了请求（{err or r.status_code}）")
    # ID token 是我们自己经 TLS 直接从 token endpoint 拿到的，不经过浏览器。OpenID Connect Core 3.1.3.7
    # 允许这种情况下用 TLS 的服务器校验代替验签，所以这里不验 JWT 签名（也就不用引入加解密库），
    # 其余各项照查：签发方、受众、有效期、nonce。
    cl = _id_token_claims(body["id_token"])
    issuers = {config.OIDC_ISSUER}
    if config.OIDC_ISSUER == "https://accounts.google.com":
        issuers.add("accounts.google.com")          # Google 两种写法都会出现
    aud = cl.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if (str(cl.get("iss", "")).rstrip("/") not in issuers or config.GOOGLE_CLIENT_ID not in auds
            or (len(auds) > 1 and cl.get("azp") != config.GOOGLE_CLIENT_ID)
            or not isinstance(cl.get("exp"), (int, float)) or cl["exp"] < time.time() - 60
            or not hmac.compare_digest(str(cl.get("nonce", "")), row["nonce"])):
        raise AuthError("provider", "登录服务返回的身份令牌没通过校验")
    email = str(cl.get("email") or "").strip().lower()
    if not email or cl.get("email_verified") not in (True, "true"):
        raise AuthError("unverified")
    if email not in config.ALLOWED_EMAILS:
        print(f"auth: 不在名单里的邮箱尝试登录：{email}", flush=True)
        raise AuthError("denied")
    return create_session(email)


# ---------- 外部登录（ROUNDTABLE_AUTH=external） ----------
#
# 给已经有一套登录网关的部署用：前面的反向代理（Caddy 的 forward_auth、nginx 的 auth_request、oauth2-proxy、Authelia……）
# 已经把没登录的请求挡在外面了。但应用不应该因此就什么都不查——有人绕过代理直接连到应用的端口、或者网关那边
# 配错了放了不该放的人，应用得自己拦得住。怎么核对因网关而异（查它的会话库、验它签的令牌、认一个只有代理知道的密钥），
# 所以做成一个小文件，由部署者自己写，放在仓库外面：
#
#   def authenticate(request) -> str | None
#       request = {"site": "chat" 或 "term", "headers": {小写的请求头: 值}, "cookies": {名字: 值}, "client": 对端地址}
#       返回用户标识（一般是邮箱）= 放行；返回 None = 没登录（401）；抛 PermissionError("原因") = 登录了但不许进（403）。
#       抛别的异常 = 当作登录服务出了问题（503），不放行。
#   LOGIN_URL = "https://…"          可选。没登录的人直接打开页面时把他送去哪（一般到不了这一步：代理先拦了）
#
# 每个请求都会调一次（事件流、终端的长连接是每 30 秒一次），所以它要快，别在里面发网络请求。
# 设了 ROUNDTABLE_ALLOWED_EMAILS / ROUNDTABLE_TERM_USERS 的话，它返回的用户还得在名单里。

_plugin = None


def plugin():
    global _plugin
    if _plugin is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("roundtable_auth_plugin", config.AUTH_PLUGIN)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not callable(getattr(mod, "authenticate", None)):
            raise RuntimeError(f"{config.AUTH_PLUGIN} 里没有 authenticate(request) 函数")
        _plugin = mod
    return _plugin


def _cookies(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        c = SimpleCookie()
        c.load(raw)
        return {k: v.value for k, v in c.items()}
    except Exception:  # noqa: BLE001
        return {m.group(1): m.group(2) for m in re.finditer(r"(?:^|;\s*)([\w.-]+)=([^;]*)", raw)}


def external_user(headers, client, site: str) -> tuple[str | None, int, str]:
    """问外部登录的那个文件。site：chat（对话）或 term（终端）。任何意外都不放行。"""
    try:
        hd = {k.lower(): v for k, v in dict(headers).items()}
        user = plugin().authenticate({"site": site, "headers": hd, "cookies": _cookies(hd.get("cookie")),
                                      "client": str(client[0]) if client else ""})
    except PermissionError as e:
        return None, 403, str(e) or "无权访问"
    except Exception as e:  # noqa: BLE001
        print(f"auth: 外部登录出错（不放行）：{type(e).__name__}: {e}", flush=True)
        return None, 503, "登录服务暂时不可用"
    if not user or not isinstance(user, str):
        return None, 401, "未登录或登录已过期"
    user = user.strip()[:200]
    if config.ALLOWED_EMAILS and user.lower() not in config.ALLOWED_EMAILS:
        return None, 403, "这个账号不在允许名单里"
    return user, 200, ""


def external_login_url() -> str:
    try:
        u = getattr(plugin(), "LOGIN_URL", "")
        return u if isinstance(u, str) and u.startswith(("https://", "http://")) else ""
    except Exception:  # noqa: BLE001
        return ""
