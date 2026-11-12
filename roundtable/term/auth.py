"""谁能进终端。

第一道：圆桌的登录。终端和对话页面是两个来源，对话那边的登录 Cookie 这里看不到（它是 __Host- 前缀的，只属于那个域名），
所以登录是“递票”过来的：
  - 对话页面（已经登录）向它自己的后端要一张一次性票据（POST /api/term/ticket，要过那边的 CSRF 检查），库里只存哈希，60 秒作废
  - 浏览器带着票据打开终端的 /auth/handoff；终端核对、作废这张票，给浏览器种一个终端自己的会话 Cookie
  - 之后每个请求都用这个 Cookie 回库里查：会话还在不在、这个人现在还许不许登录（名单改了立刻生效）
ROUNDTABLE_AUTH=none 时没有登录：只接受本机回环直连，规则和对话那边一样。

第二道：动态验证码，标准 TOTP（RFC 6238，SHA-1、30 秒、6 位），谷歌验证器、1Password 之类都能扫。每个用户各绑各的。
  - 密钥只在服务器的 <数据目录>/term/totp.json 里（600）。第一次由网页绑定；绑定之后网页上不能重绑，
    要重置只能在服务器上：python -m roundtable term totp-reset [用户]
  - 验证通过发一个只给本站的 Cookie（HttpOnly、SameSite=Strict），库里只存它的哈希。最长 UNLOCK_TTL，闲置 UNLOCK_IDLE 就失效
  - 连错 TOTP_MAX_FAILS 次锁 TOTP_LOCKOUT 秒；同一个验证码用过一次就作废
  - ROUNDTABLE_TERM_TOTP=off 时没有这一道
WebSocket 另外要一张一次性票据（ticket_issue），见 app.py 里的说明。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
import urllib.parse

from .. import auth as rt_auth
from .. import config as rt
from .. import db as rt_db
from . import config, db

COOKIE = "term_unlock"
# __Host- 前缀：浏览器只在 HTTPS、Path=/、不带 Domain 时才接受，别的子域名没法替你种一个
SESSION_COOKIE = "__Host-rt_term" if config.HTTPS else "rt_term"
TOTP_FILE = config.DATA / "totp.json"
OPEN_PREFIX = "open:"             # 没开动态验证码时用的“解锁会话”标识


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- 第一道：圆桌的登录 ----------

def allowed(user: str | None) -> bool:
    """这个（已经能登录圆桌的）人许不许进终端。"""
    if not user:
        return False
    return not rt.TERM_USERS or rt.AUTH_MODE != "google" or user in rt.TERM_USERS


def identify(headers, client, token: str | None) -> tuple[str | None, int, str]:
    """→ (user, status, 提示)。user 为 None 就是拒绝。"""
    if rt.AUTH_MODE == "none":
        why = rt_auth.local_request_problem(headers, client)
        return (None, 403, why) if why else (rt_auth.LOCAL_USER, 200, "")
    user = rt_auth.session_user(token)
    if not user:
        return None, 401, "未登录或登录已过期"
    return (user, 200, "") if allowed(user) else (None, 403, "这个账号不能进终端")


def session_user(token: str | None) -> str | None:
    """已经连上的终端定期复查用：会话还在、人还在名单里。不登录的模式没有会话可查。"""
    if rt.AUTH_MODE == "none":
        return rt_auth.LOCAL_USER
    user = rt_auth.session_user(token)
    return user if allowed(user) else None


def origin_ok(origin: str | None, host: str | None) -> bool:
    if origin == config.ORIGIN:
        return True
    # 本机模式允许换端口（SSH 隧道常见）：Origin 要和 Host 一致，且 Host 是回环地址
    return bool(rt.AUTH_MODE == "none" and origin and host and origin == f"http://{host}" and rt_auth._host_is_local(host))


def handoff_issue(user: str) -> str:
    """（对话那边的进程调）给已经登录的 user 开一张去终端的一次性票据。"""
    token = secrets.token_urlsafe(32)
    now = time.time()
    rt_db.x("DELETE FROM term_tickets WHERE expires_at<?", (now,))
    rt_db.x("INSERT INTO term_tickets(id,user,expires_at) VALUES(?,?,?)", (_h(token), user, now + config.HANDOFF_TTL))
    return token


def handoff_redeem(token: str | None) -> str | None:
    """票据换成终端自己的会话。→ 会话 Cookie 的值；票据不对、过期、用过了，或者这个人已经不许登录了 → None。"""
    if not token or len(token) > 200:
        return None
    r = rt_db.q1("SELECT user, expires_at FROM term_tickets WHERE id=?", (_h(token),))
    # 删到了才算数：一张票只能用一次
    if not r or rt_db.con().execute("DELETE FROM term_tickets WHERE id=?", (_h(token),)).rowcount != 1:
        return None
    user = r["user"]
    ok = (rt.AUTH_MODE == "password" and user == rt_auth.OWNER_USER) or (rt.AUTH_MODE == "google" and user in rt.ALLOWED_EMAILS)
    if r["expires_at"] < time.time() or not ok or not allowed(user):
        return None
    return rt_auth.create_session(user)


def session_cookie(token: str, clear: bool = False) -> str:
    secure = "; Secure" if config.HTTPS else ""
    age = 0 if clear else rt.SESSION_DAYS * 86400
    return f"{SESSION_COOKIE}={'' if clear else token}; Path=/; Max-Age={age}; HttpOnly; SameSite=Lax{secure}"


def session_drop(token: str | None) -> None:
    rt_auth.destroy_session(token)


# ---------- 动态验证码 ----------

def _code(secret: bytes, counter: int) -> str:
    mac = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    return f"{(struct.unpack('>I', mac[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def _load() -> dict:
    """→ {用户: {secret, enrolled_at, last_counter, pending, pending_at}}"""
    try:
        d = json.loads(TOTP_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    config.DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = TOTP_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, TOTP_FILE)


def enrolled(user: str) -> bool:
    return bool(_load().get(user, {}).get("secret"))


def enrolled_users() -> list[str]:
    return [u for u, v in _load().items() if v.get("secret")]


def otpauth_uri(secret_b32: str, user: str) -> str:
    label = urllib.parse.quote(f"{config.TOTP_ISSUER}:{user}")
    return (f"otpauth://totp/{label}?secret={secret_b32}&issuer={urllib.parse.quote(config.TOTP_ISSUER)}"
            "&algorithm=SHA1&digits=6&period=30")


def enroll_start(user: str) -> str:
    """生成一个待确认的密钥（还没生效），→ base32。已经绑定过就抛 PermissionError。"""
    d = _load()
    me = d.get(user, {})
    if me.get("secret"):
        raise PermissionError("已经绑定过验证器了。要重新绑定，在服务器上运行 python -m roundtable term totp-reset")
    me = {"pending": base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="), "pending_at": time.time()}
    d[user] = me
    _save(d)
    return me["pending"]


def enroll_confirm(user: str, code: str) -> bool:
    """用验证器里的码确认待绑定的密钥。对了才生效。"""
    d = _load()
    me = d.get(user, {})
    if me.get("secret") or not me.get("pending") or time.time() - me.get("pending_at", 0) > 900:
        return False
    ok, counter = _match(me["pending"], code)
    if not ok:
        return False
    d[user] = {"secret": me["pending"], "enrolled_at": time.time(), "last_counter": counter}   # 这个码也算用过了
    _save(d)
    return True


def _match(secret_b32: str, code: str, after: int = 0) -> tuple[bool, int]:
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != 6:
        return False, 0
    secret = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    now = int(time.time()) // 30
    for c in (now, now - 1, now + 1):                # 前后各容一个 30 秒，手机和服务器的钟不会完全一样
        if c > after and hmac.compare_digest(_code(secret, c), code):
            return True, c
    return False, 0


def _fail_key(user: str) -> str:
    return "totp_fail:" + user


def locked_for(user: str) -> int:
    """验证码连错太多次：还要锁多少秒。0 = 没锁。"""
    st = db.get(_fail_key(user), {})
    return max(0, int(st.get("until", 0) - time.time()))


def verify(user: str, code: str) -> bool:
    """验证一个码。会记失败次数、防重放。"""
    if locked_for(user):
        return False
    d = _load()
    me = d.get(user, {})
    if not me.get("secret"):
        return False
    ok, counter = _match(me["secret"], code, after=int(me.get("last_counter", 0)))
    st = db.get(_fail_key(user), {})
    if ok:
        me["last_counter"] = counter
        d[user] = me
        _save(d)
        db.put(_fail_key(user), {})
        return True
    n = int(st.get("n", 0)) + 1
    db.put(_fail_key(user), {"n": 0, "until": time.time() + config.TOTP_LOCKOUT} if n >= config.TOTP_MAX_FAILS else {"n": n})
    return False


def reset(user: str | None = None) -> list[str]:
    """删掉验证器的绑定和解锁会话（只从命令行调用）。不给 user 就是所有人。→ 被重置的用户。"""
    d = _load()
    who = [user] if user else list(d)
    for u in who:
        d.pop(u, None)
        db.x("DELETE FROM unlocks WHERE email=?", (u,))
        db.put(_fail_key(u), {})
    if d:
        _save(d)
    else:
        try:
            TOTP_FILE.unlink()
        except FileNotFoundError:
            pass
        db.x("DELETE FROM unlocks")
    return who


# ---------- 解锁会话（Cookie） ----------

def unlock_create(user: str, ip: str, ua: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.x("DELETE FROM unlocks WHERE created_at<? OR last_active<?", (now - config.UNLOCK_TTL, now - config.UNLOCK_IDLE))
    db.x("INSERT INTO unlocks(token_hash,email,created_at,last_active,ip,ua) VALUES(?,?,?,?,?,?)",
         (_h(token), user, now, now, ip[:80], ua[:200]))
    return token


def unlock_check(token: str | None, user: str, session_token: str | None = None) -> dict | None:
    """有效 → {"hash", "expires_in", "idle_left"}；无效（没有、过期、闲置太久、换了账号）→ None。
    没开动态验证码时永远有效，标识跟着登录会话走（这样退出登录时还能把这个人的终端连接断掉）。"""
    if not config.TOTP:
        return {"hash": OPEN_PREFIX + _h(f"{user}\n{session_token or ''}")[:40], "expires_in": 0, "idle_left": 0}
    if not token or len(token) > 200:
        return None
    r = db.q1("SELECT * FROM unlocks WHERE token_hash=?", (_h(token),))
    now = time.time()
    if not r or r["email"] != user:
        return None
    left, idle = r["created_at"] + config.UNLOCK_TTL - now, r["last_active"] + config.UNLOCK_IDLE - now
    if left <= 0 or idle <= 0:
        db.x("DELETE FROM unlocks WHERE token_hash=?", (r["token_hash"],))
        return None
    return {"hash": r["token_hash"], "expires_in": int(left), "idle_left": int(idle)}


def unlock_touch(token_hash: str) -> None:
    """有操作了：闲置计时从头算。"""
    if not token_hash.startswith(OPEN_PREFIX):
        db.x("UPDATE unlocks SET last_active=? WHERE token_hash=?", (time.time(), token_hash))


def unlock_drop(token: str | None) -> None:
    if token:
        db.x("DELETE FROM unlocks WHERE token_hash=?", (_h(token),))


def unlock_alive(token_hash: str) -> bool:
    """给已经连上的终端定期复查用（不看 Cookie，只看库里这条还在不在、过没过期）。"""
    if token_hash.startswith(OPEN_PREFIX):
        return not config.TOTP
    r = db.q1("SELECT created_at, last_active FROM unlocks WHERE token_hash=?", (token_hash,))
    now = time.time()
    return bool(r and r["created_at"] + config.UNLOCK_TTL > now and r["last_active"] + config.UNLOCK_IDLE > now)


def cookie_header(token: str, clear: bool = False) -> str:
    secure = "; Secure" if config.HTTPS else ""
    if clear:
        return f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}"
    return f"{COOKIE}={token}; Path=/; Max-Age={config.UNLOCK_TTL}; HttpOnly; SameSite=Strict{secure}"


# ---------- WebSocket 的一次性票据 ----------

_tickets: dict[str, tuple[str, float]] = {}


def ticket_issue(unlock_hash: str) -> str:
    now = time.time()
    for k in [k for k, (_, exp) in _tickets.items() if exp < now]:
        _tickets.pop(k, None)
    t = secrets.token_urlsafe(24)
    _tickets[t] = (unlock_hash, now + config.WS_TICKET_TTL)
    return t


def ticket_use(ticket: str | None, unlock_hash: str) -> bool:
    """票据只能用一次，而且必须是同一个解锁会话换来的。"""
    v = _tickets.pop(ticket or "", None)
    return bool(v and v[0] == unlock_hash and v[1] >= time.time())
