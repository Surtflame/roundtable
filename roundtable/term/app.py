"""网页终端：FastAPI + 最外层的 Guard（两道门、CSRF、安全头）+ 路由。

谁能进来（每个请求都查，包括静态文件和 WebSocket 握手）：
  1. 圆桌的登录（auth.identify：终端自己的会话 Cookie，是从对话页面递票换来的；不登录的模式只认本机回环直连）
  2. 动态验证码换来的解锁 Cookie（auth.unlock_check）——除了锁屏页面要用的静态文件和 /api/auth/*；没开验证码时没有这一道
WebSocket 额外的规矩（握手之后就是一条长连接，不会再过上面的检查，所以这里自己管）：
  - 握手时查 Origin 必须是本站：浏览器发起 WebSocket 会自动带 Cookie，而且不受同源策略限制，不查就能被别的网站借用
  - 握手的地址里要带一张一次性票据，票据得先用普通请求（过得了 CSRF 检查的）换，30 秒内用掉
  - 连上以后每 30 秒复查一次登录和解锁状态，失效就断（ptyws.recheck）；锁定、退出时立刻全断
"""
import html
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from pathlib import Path

import segno
from fastapi import FastAPI, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders

from .. import config as rt
from .. import db as rt_db
from . import auth, config, db, envs, fsapi, history, ptyws, remote, tmuxctl

BOOT = secrets.token_hex(4)
WS_ORIGIN = config.ORIGIN.replace("https://", "wss://").replace("http://", "ws://")
# 圆桌那个站（“对话 | 终端”的另一边）。为了切换时不用重新加载，两个站可以互相把对方放在 iframe 里：
# 只许对方那一个来源嵌（frame-ancestors），自己也只嵌对方（frame-src）。隔着来源，嵌进来的页面碰不到这边的脚本和内容。
# 本机模式下地址栏里可能是 localhost 也可能是 127.0.0.1（还可能换了端口），没法写死，就不限制谁来嵌——反正只有本机连得上。
_peer = config.CHAT_ORIGIN if rt.AUTH_MODE != "none" else "http://localhost:* http://127.0.0.1:*"
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       f"font-src 'self'; connect-src 'self' {WS_ORIGIN if rt.AUTH_MODE != 'none' else 'ws://localhost:* ws://127.0.0.1:*'}; "
       f"frame-ancestors {_peer}; frame-src {_peer}; base-uri 'none'; form-action 'self'; object-src 'none'")
CSRF_HEADER = "roundtable-term"
OPEN_PATHS = ("/api/auth/", "/static/")          # 没解锁也能访问的：锁屏页面自己要用
NO_LOGIN = ("/healthz", "/auth/handoff")         # 没登录也能访问的


def _cookies(headers: Headers) -> dict:
    raw = headers.get("cookie")
    if not raw:
        return {}
    try:
        c = SimpleCookie()
        c.load(raw)
        return {k: v.value for k, v in c.items()}
    except Exception:  # noqa: BLE001
        return {m.group(1): m.group(2) for m in re.finditer(r"(?:^|;\s*)([\w.-]+)=([^;]*)", raw)}


def _ip(headers: Headers, scope) -> str:
    xff = headers.get("x-forwarded-for")
    return (xff.split(",")[0].strip() if xff else (scope.get("client") or ("", 0))[0])[:80]


def _need_login_page() -> bytes:
    """没登录时打开终端看到的页面：登录在对话那边做，这里只给一个过去的链接。"""
    url = html.escape(config.CHAT_URL + "?term=1", quote=True)
    return ("<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>终端</title><style>body{font:16px/1.6 system-ui,sans-serif;margin:0;min-height:100vh;display:grid;place-items:center;"
            "background:#f7f6f3;color:#1f1e1c}@media(prefers-color-scheme:dark){body{background:#1d1d1b;color:#ecebe7}a{color:#a9a6ff}}"
            "main{max-width:26em;padding:24px;text-align:center}a{color:#5b54d6}</style>"
            f"<main><h1 style=\"font-size:20px\">要先登录</h1><p>终端用的是圆桌的登录。</p><p><a href=\"{url}\" target=\"_top\">去圆桌登录，然后回到终端</a></p></main></html>"
            ).encode()


class Guard:
    """最外层 ASGI 中间件。"""

    def __init__(self, app):
        self.app = app

    async def _deny(self, send, status: int, msg: str, extra: dict | None = None) -> None:
        body = json.dumps({"error": msg, **(extra or {})}, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json; charset=utf-8"), (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        cookies = _cookies(headers)
        sid = auth.credential(headers, scope.get("client"), cookies)
        if scope["type"] == "websocket":
            # 握手没过就在 accept 之前关掉，浏览器那边看到的是 403
            user, _, _ = auth.identify(headers, scope.get("client"), sid)
            unlock = auth.unlock_check(cookies.get(auth.COOKIE), user, sid) if user else None
            if not auth.origin_ok(headers.get("origin"), headers.get("host")) or not user or not unlock:
                await receive()
                return await send({"type": "websocket.close", "code": 1008})
            scope.setdefault("state", {}).update(email=user, unlock=unlock, sid=sid, ip=_ip(headers, scope))
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return
        path, method = scope["path"], scope["method"]
        if method == "OPTIONS":
            return await self._deny(send, 405, "不支持")
        if path not in NO_LOGIN:
            user, status, why = auth.identify(headers, scope.get("client"), sid)
            if not user:
                if status == 401 and path == "/" and method == "GET":        # 浏览器直接打开：给一个去登录的页面
                    body = _need_login_page()
                    await send({"type": "http.response.start", "status": 401, "headers": [
                        (b"content-type", b"text/html; charset=utf-8"), (b"cache-control", b"no-store"),
                        (b"content-security-policy", f"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors {_peer}".encode()),
                        (b"x-content-type-options", b"nosniff"), (b"content-length", str(len(body)).encode())]})
                    return await send({"type": "http.response.body", "body": body})
                return await self._deny(send, status, why)
            if method not in ("GET", "HEAD"):
                if (headers.get("x-requested-with") != CSRF_HEADER or not auth.origin_ok(headers.get("origin"), headers.get("host"))
                        or headers.get("sec-fetch-site", "same-origin") != "same-origin"):
                    return await self._deny(send, 403, "跨站请求被拒绝")
            unlock = auth.unlock_check(cookies.get(auth.COOKIE), user, sid)
            if not unlock and path != "/" and not path.startswith(OPEN_PATHS):
                return await self._deny(send, 423, "需要输入动态验证码", {"locked": True})
            if unlock and method not in ("GET", "HEAD"):
                auth.unlock_touch(unlock["hash"])            # 有操作：闲置计时从头算（轮询用的 GET 不算）
            scope.setdefault("state", {}).update(email=user, unlock=unlock, ip=_ip(headers, scope), sid=sid,
                                                 unlock_token=cookies.get(auth.COOKIE))

        async def send2(msg):
            if msg["type"] == "http.response.start":
                h = MutableHeaders(scope=msg)
                h["X-Content-Type-Options"] = "nosniff"
                h["Referrer-Policy"] = "no-referrer"
                # 谁能把这个站嵌进 iframe 由 CSP 的 frame-ancestors 管（只许圆桌），所以不发 X-Frame-Options。
                # 前面的反向代理要是自己加了 X-Frame-Options: DENY，有 frame-ancestors 时浏览器按规范忽略它
                h["Cross-Origin-Opener-Policy"] = "same-origin"
                h["Cross-Origin-Resource-Policy"] = "same-origin"
                h["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
                if "content-security-policy" not in h:
                    h["Content-Security-Policy"] = CSP
                if config.HTTPS:
                    h["Strict-Transport-Security"] = "max-age=31536000"
                if "cache-control" not in h:
                    # 静态文件：浏览器可以留着，每次用之前问一句变没变（没变就是一个很小的 304）；别的都不许存
                    h["Cache-Control"] = "no-cache" if path.startswith("/static/") else "no-store"
            await send(msg)
        await self.app(scope, receive, send2)


@asynccontextmanager
async def lifespan(_app):
    for w in rt.check():
        print("提醒：" + w, flush=True)
    db.con()
    rt_db.con()
    try:
        await tmuxctl.ensure_server()
        up = True
    except tmuxctl.TmuxError as e:
        up = False
        print("提醒：", e, flush=True)
    who = ("不用登录，只接受本机访问" if rt.AUTH_MODE == "none" else "登录归外部的登录网关" if rt.AUTH_MODE == "external"
           else f"用圆桌的登录（{rt.AUTH_MODE}）")
    print(f"圆桌的终端：{config.ORIGIN}（监听 {config.HOST}:{config.PORT}，tmux -L {config.TMUX_SOCKET}"
          f"{'' if up else '，还没起来'}；{who}；动态验证码{'开着' if config.TOTP else '没开'}）", flush=True)
    yield
    await ptyws.close_all(code=1001)


api = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def bad(msg: str, status: int = 400):
    raise HTTPException(status, msg)


@api.exception_handler(HTTPException)
async def _http_exc(_req, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@api.exception_handler(tmuxctl.TmuxError)
async def _tmux_exc(_req, exc: tmuxctl.TmuxError):
    return JSONResponse({"error": str(exc)}, status_code=400)


@api.exception_handler(fsapi.FsError)
async def _fs_exc(_req, exc: fsapi.FsError):
    return JSONResponse({"error": str(exc)}, status_code=exc.status)


async def body_json(request: Request) -> dict:
    try:
        d = await request.json()
    except Exception:  # noqa: BLE001
        bad("请求体不是合法的 JSON")
    if not isinstance(d, dict):
        bad("请求体格式不对")
    return d


def st(request: Request) -> dict:
    return request.scope["state"]


@api.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")


@api.get("/")
async def index():
    page = (config.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    page = re.sub(r'((?:href|src)="/static/[^"?]+)"', rf'\1?v={BOOT}"', page)
    return HTMLResponse(page)


# ---------- 登录：从对话页面递票过来 ----------

@api.get("/auth/handoff")
async def handoff(request: Request, t: str = ""):
    """对话页面带着一次性票据把浏览器送到这里：票据对就种下终端自己的会话 Cookie，然后去首页。
    票据不对（过期、用过了）也去首页——那里会显示“要先登录”。"""
    r = RedirectResponse("/", status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    if rt.AUTH_MODE in ("password", "google"):
        token = auth.handoff_redeem(t)
        if token:
            r.headers.append("Set-Cookie", auth.session_cookie(token))
            db.audit("login", "handoff", _ip(request.headers, request.scope))
    return r


@api.post("/api/auth/logout")
async def logout(request: Request):
    """退出终端这边的登录：断开这个人的终端连接，作废解锁和会话。对话那边的登录不受影响。"""
    s = st(request)
    if s["unlock"]:
        await ptyws.close_all(s["unlock"]["hash"])
    if rt.AUTH_MODE not in ("password", "google"):
        bad("这种登录方式下终端没有自己的登录可退", 409)
    auth.unlock_drop(s.get("unlock_token"))
    auth.session_drop(s.get("sid"))
    db.audit("logout", "", s["ip"])
    r = JSONResponse({"ok": True})
    r.headers.append("Set-Cookie", auth.cookie_header("", clear=True))
    r.headers.append("Set-Cookie", auth.session_cookie("", clear=True))
    return r


# ---------- 动态验证码 ----------

def _auth_state(request: Request) -> dict:
    u, user = st(request)["unlock"], st(request)["email"]
    return {"email": user, "totp": config.TOTP, "enrolled": auth.enrolled(user) if config.TOTP else True, "unlocked": bool(u),
            "expires_in": u["expires_in"] if u else 0, "idle_left": u["idle_left"] if u else 0,
            "locked_for": auth.locked_for(user) if config.TOTP else 0, "boot": BOOT, "chat_url": config.CHAT_URL,
            "auth_mode": rt.AUTH_MODE}


@api.get("/api/auth/state")
async def auth_state(request: Request):
    return _auth_state(request)


@api.post("/api/auth/enroll/start")
async def enroll_start(request: Request):
    if not config.TOTP:
        bad("没有开动态验证码", 404)
    user = st(request)["email"]
    try:
        secret = auth.enroll_start(user)
    except PermissionError as e:
        bad(str(e), 403)
    uri = auth.otpauth_uri(secret, user)
    svg = segno.make(uri, error="m").svg_inline(scale=5, border=2, dark="#111", light="#fff")
    db.audit("enroll_start", user, st(request)["ip"])
    return {"secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)), "qr": svg}


def _unlock_response(request: Request, kind: str) -> JSONResponse:
    s = st(request)
    token = auth.unlock_create(s["email"], s["ip"], request.headers.get("user-agent", ""))
    db.audit(kind, s["email"], s["ip"])
    r = JSONResponse({"ok": True})
    r.headers.append("Set-Cookie", auth.cookie_header(token))
    return r


@api.post("/api/auth/enroll/confirm")
async def enroll_confirm(request: Request):
    d = await body_json(request)
    if not config.TOTP or not auth.enroll_confirm(st(request)["email"], str(d.get("code") or "")):
        bad("验证码不对（或者二维码已经过期，刷新页面重新来）")
    return _unlock_response(request, "enroll")


@api.post("/api/auth/unlock")
async def unlock(request: Request):
    d = await body_json(request)
    user = st(request)["email"]
    if not config.TOTP:
        return JSONResponse({"ok": True})
    left = auth.locked_for(user)
    if left:
        bad(f"错太多次了，{left // 60 + 1} 分钟后再试", 429)
    if not auth.enrolled(user):
        bad("还没绑定验证器", 409)
    if not auth.verify(user, str(d.get("code") or "")):
        db.audit("unlock_fail", user, st(request)["ip"])
        left = auth.locked_for(user)
        bad(f"错太多次了，{left // 60 + 1} 分钟后再试" if left else "验证码不对", 429 if left else 400)
    return _unlock_response(request, "unlock")


@api.post("/api/auth/lock")
async def lock(request: Request):
    s = st(request)
    if not config.TOTP:
        bad("没有开动态验证码，没有可锁的", 409)
    if s["unlock"]:
        await ptyws.close_all(s["unlock"]["hash"])
    auth.unlock_drop(s.get("unlock_token"))
    db.audit("lock", "", s["ip"])
    r = JSONResponse({"ok": True})
    r.headers.append("Set-Cookie", auth.cookie_header("", clear=True))
    return r


# ---------- 基本信息 ----------

@api.get("/api/me")
async def me(request: Request):
    return {**_auth_state(request), "home": config.HOME, "default_cwd": config.DEFAULT_CWD,
            "tmux_socket": config.TMUX_SOCKET, "roots": fsapi.roots(),
            # 别的机器上的环境（新窗口菜单里可以选）。只给名字和目录，连接方式不出服务器
            "envs": envs.public(),
            "limits": {"edit": config.MAX_EDIT, "upload": config.MAX_UPLOAD, "idle": config.UNLOCK_IDLE,
                       "ttl": config.UNLOCK_TTL}}


# ---------- 窗口 ----------

async def _scene(refresh_remote: bool = False) -> dict:
    """窗口和正在跑的 Claude 进程对到一起：哪个窗口里是哪个对话，每个对话现在由谁带着、在哪儿。
    → {"windows": [...], "claude": {对话 id: 条目}, "at": {对话 id: 它在哪}, "procs": [...], "extra": 别的机器上的会话}
    别的机器（envs.py 里的清单）上的会话和进程用的是上次扫到的那份；refresh_remote=True 时过期了会在后台重扫（不等它）。"""
    ws = await tmuxctl.list_windows()
    ps = history.procs()
    web = await tmuxctl.panes()
    dft = await tmuxctl.panes(ext=True)
    alive = set(dft.values())
    for w in [w for w in ws if w["ext"] and w["ext"] not in alive]:     # 借来的窗口，那边的会话已经没了：占位记录也收掉
        try:
            await tmuxctl.kill(w["name"])
        except tmuxctl.TmuxError:
            pass
        ws.remove(w)
    ext_win = {w["ext"]: w["name"] for w in ws if w["ext"]}
    for p in ps:
        # 进程在哪：窗格里直接跑着的那个程序（多半是 bash -lc）是它的父进程，才算“在这个窗格里”。
        # 光看 Claude 自己记的 tmux 名字不行——Remote Control 后台的子进程会从服务那里继承到同一个名字
        if p["server"]:
            p["place"] = "server"
        elif p["ppid"] in web or p["pid"] in web:
            p["place"], p["win"] = "web", web.get(p["ppid"]) or web.get(p["pid"])
        elif (t := dft.get(p["ppid"]) or dft.get(p["pid"])) and tmuxctl.EXT_RE.match(t):
            p["place"], p["target"] = "tmux", t
        else:
            p["place"] = "other"
    extra = []
    for key in envs.all():
        c = remote.scan_cached(key, refresh=refresh_remote)
        names = {w["name"] for w in ws if w["env"] == key}
        for p in c["procs"]:
            # 那边我们用的是单独的 tmux 套接字，会话名又是随机的 w+十六进制，按 Claude 自己记的 tmux 名字对就够了
            p = {**p, **({"place": "server"} if p["server"] else {"place": "web", "win": p["tmux"]} if p["tmux"] in names
                         else {"place": "other"})}
            ps.append(p)
        extra += c["sessions"]
    hold = history.holders(ps)
    claude = {x["id"]: x for x in history.enrich(history.claude_sessions() + extra, ps)}
    in_win: dict = {}
    for p in sorted(ps, key=lambda p: p["started"]):
        if p["place"] == "web":
            in_win[p["win"]] = p
        elif p["place"] == "tmux" and p["target"] in ext_win:
            in_win[ext_win[p["target"]]] = p
    for w in ws:
        p = in_win.get(w["name"])
        if not p or not (w["kind"] == "ext" or (w["kind"] in ("agent", "remote") and w["agent"] == "claude")):
            if w["kind"] in ("agent", "remote") and w["agent"] == "claude" and w["asid"] in hold:
                w["dup"] = True                               # 刚另开的一份，进程还没登记上：正主在别处
            continue
        w["asid"] = p["sid"]                                  # 新开的窗口一开始没有对话 id，聊起来之后就有了
        w["rc"] = bool(p["cloud"])                            # 这个进程连着 Claude 应用
        w["dup"] = hold.get(p["sid"]) is not p                # 这个对话的正主是别的进程，这里只是另开的一份
        x = claude.get(p["sid"])
        if x and (x["named"] or x["rc"]) and x["title"]:
            w["session_title"] = x["title"]
    at = {}
    for sid, p in hold.items():
        copy = next((w["name"] for w in ws if w["asid"] == sid and w.get("dup")), None)
        if p["place"] == "web":
            at[sid] = {"win": p["win"]}
        elif p["place"] == "tmux":
            at[sid] = {"win": ext_win[p["target"]]} if p["target"] in ext_win else {"attach": p["target"]}
        else:
            at[sid] = {p["place"]: True, **({"win": copy, "copy": True} if copy else {})}
    return {"windows": ws, "claude": claude, "at": at, "procs": ps, "extra": extra}


async def _windows() -> list[dict]:
    return (await _scene())["windows"]


@api.get("/api/windows")
async def windows():
    # envs 也带上：远程环境可以随时增删（比如刚用“添加远程环境”配好一个），页面靠这个轮询跟上
    return {"windows": await _windows(), "now": time.time(), "envs": envs.public()}


# ---------- 远程环境 ----------

@api.post("/api/envs/assistant")
async def env_assistant(request: Request):
    """“添加远程环境”：在这台机器上开一个 Claude Code 对话，带着用户把另一台机器接进来（提示词见 prompts/add_env.md）。
    真正的活（生成钥匙、装公钥、登记）是那个对话里的 Claude Code 在终端里干的，这里只负责把它叫起来。"""
    tpl = (Path(__file__).resolve().parent / "prompts" / "add_env.md").read_text(encoding="utf-8")
    text = tpl.replace("{root}", str(config.ROOT)).replace("{python}", sys.executable).replace("{module}", "roundtable term")
    pdir = config.DATA / "prompts"
    pdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pfile = pdir / "add_env.md"
    pfile.write_text(text, encoding="utf-8")
    name = await tmuxctl.create("agent", config.DEFAULT_CWD if os.path.isdir(config.DEFAULT_CWD) else config.HOME, "添加远程环境", "claude",
                                prompt_file=str(pfile), first="我想添加一个远程环境。")
    db.audit("window", f"new {name} env-assistant", st(request)["ip"])
    return {"name": name, "windows": await _windows()}


@api.delete("/api/envs/{key}")
async def env_remove(key: str, request: Request):
    """从清单里拿掉一个远程环境。只是这边不再列它：钥匙文件、那台机器上装的公钥都不动。"""
    if not envs.get(key):
        bad("没有这个环境", 404)
    if any(w["env"] == key for w in await tmuxctl.list_windows()):
        bad("这个环境上还有开着的窗口，先关掉", 409)
    envs.remove(key)
    db.audit("env", f"remove {key}", st(request)["ip"])
    return {"envs": envs.public()}


@api.post("/api/windows")
async def window_new(request: Request):
    d = await body_json(request)
    kind = "agent" if d.get("agent") in ("claude", "codex") else "shell"
    rc = bool(d.get("rc")) and d.get("agent") == "claude"
    envk = str(d.get("env") or "")
    if envk:                                                  # 开在别的机器上
        if not envs.get(envk):
            bad("没有这个环境")
        name = await remote.create(envk, kind, str(d.get("title") or ""), d.get("agent") or "", rc=rc)
        remote.invalidate(envk)
        db.audit("window", f"new {name} @{envk} {kind}:{d.get('agent') or ''}{' rc' if rc else ''}", st(request)["ip"])
        return {"name": name, "windows": await _windows()}
    cwd = fsapi.norm(d.get("cwd") or config.DEFAULT_CWD)
    name = await tmuxctl.create(kind, cwd, str(d.get("title") or ""), d.get("agent") or "", rc=rc)
    db.audit("window", f"new {name} {kind}:{d.get('agent') or ''}{' rc' if rc else ''} {cwd}", st(request)["ip"])
    return {"name": name, "windows": await _windows()}


@api.patch("/api/windows/{name}")
async def window_rename(name: str, request: Request):
    d = await body_json(request)
    await tmuxctl.rename(name, str(d.get("title") or ""))
    return {"windows": await _windows()}


@api.delete("/api/windows/{name}")
async def window_kill(name: str, request: Request):
    where, tgt = await tmuxctl.target(name)
    if where == "remote":                                     # 别的机器上的窗口：那边的也关掉（借来的窗口不关那边，只删占位记录）
        await remote.kill(tgt, name)
        remote.invalidate(tgt)
    await tmuxctl.kill(name)
    db.audit("window", f"kill {name}", st(request)["ip"])
    return {"windows": await _windows()}


@api.post("/api/ws-ticket")
async def ws_ticket(request: Request):
    return {"ticket": auth.ticket_issue(st(request)["unlock"]["hash"])}


@api.websocket("/ws/term/{name}")
async def ws_term(ws: WebSocket, name: str, t: str = "", cols: int = 120, rows: int = 32):
    s = ws.scope["state"]
    if not auth.ticket_use(t, s["unlock"]["hash"]) or not tmuxctl.NAME_RE.match(name):
        return await ws.close(1008)
    await ws.accept()
    if not await tmuxctl.exists(name):
        return await ws.close(ptyws.CLOSE_GONE)
    db.audit("ws", f"attach {name}", s["ip"])
    await ptyws.serve(ws, name, cols, rows, s["unlock"]["hash"], s["sid"], s["email"])


# ---------- 历史对话 ----------

@api.get("/api/history")
async def history_list(q: str = "", hidden: int = 0):
    sc = await _scene(refresh_remote=True)
    open_in = {(w["agent"], w["asid"]): w["name"] for w in sc["windows"] if w["asid"] and not w.get("dup")}
    items, n_hidden = history.sessions(q, bool(hidden), ps=sc["procs"], extra=sc["extra"])
    out = []
    for s in items:
        # at：这个对话现在在哪。{"win": 窗口名}＝在我们的某个窗口里（带 copy 的是另开的副本，正主在别处）；
        # {"attach": tmux 会话名}＝在这台机器别的 tmux 里跑着，可以直接接进去；{"server": true}＝Remote Control 后台带着（没有终端界面）；
        # {"other": true}＝别的终端里（screen 之类，接不进去）；空＝没在跑
        at = sc["at"].get(s["id"]) if s["agent"] == "claude" else None
        if not at and (s["agent"], s["id"]) in open_in:
            at = {"win": open_in[(s["agent"], s["id"])]}
        out.append({**s, "at": at or {}})
    # envs：别的机器上的那部分是什么时候扫的、上次有没有连上
    env_state = {k: {"at": c["at"], "error": c.get("error") or ""} for k in envs.all() for c in [remote.scan_cached(k, refresh=False)]}
    return {"sessions": out, "hidden_count": n_hidden, "cloud": bool(history.cloud()), "envs": env_state}


@api.post("/api/history/pin")
async def history_pin(request: Request):
    d = await body_json(request)
    agent, sid = d.get("agent"), str(d.get("id") or "")
    if agent not in ("claude", "codex") or not tmuxctl.ID_RE.match(sid):
        bad("参数不对")
    history.set_pin(agent, sid, bool(d.get("pinned")))
    return {"ok": True}


@api.post("/api/history/hide")
async def history_hide(request: Request):
    d = await body_json(request)
    agent, sid = d.get("agent"), str(d.get("id") or "")
    if agent not in ("claude", "codex") or not tmuxctl.ID_RE.match(sid):
        bad("参数不对")
    history.set_hidden(agent, sid, bool(d.get("hidden")))
    return {"ok": True}


@api.post("/api/history/attach")
async def history_attach(request: Request):
    """对话正跑在这台机器别的 tmux 会话里（终端里开的、带 Remote Control 的 Claude Code）：不另起进程，直接接上去。
    接哪个会话由服务器按对话 id 自己查，不收浏览器给的会话名。"""
    d = await body_json(request)
    sid = str(d.get("id") or "")
    if not tmuxctl.ID_RE.match(sid):
        bad("参数不对")
    sc = await _scene()
    at = sc["at"].get(sid) or {}
    if at.get("win") and not at.get("copy"):
        return {"name": at["win"], "existing": True}
    if not at.get("attach"):
        bad("这个对话现在不在能接进去的终端里", 409)
    x = sc["claude"].get(sid)
    name = await tmuxctl.create_ext(at["attach"], (x["title"] if x and (x["named"] or x["rc"]) else "")[:60])
    db.audit("window", f"attach {name} -> tmux:{at['attach']} claude:{sid}", st(request)["ip"])
    return {"name": name, "existing": False}


@api.post("/api/history/resume")
async def history_resume(request: Request):
    d = await body_json(request)
    agent, sid = d.get("agent"), str(d.get("id") or "")
    if agent not in ("claude", "codex") or not tmuxctl.ID_RE.match(sid):
        bad("参数不对")
    sc = await _scene()
    for w in sc["windows"]:
        if w["agent"] == agent and w["asid"] == sid and w["kind"] != "ext":
            return {"name": w["name"], "existing": True}          # 已经在某个窗口里开着了：直接跳过去
    at = sc["at"].get(sid) if agent == "claude" else None
    if at and not d.get("fork"):
        # 有别的进程带着它：再起一个就是两份各聊各的。浏览器那边问过用户、明确要另开一份才放行
        bad("这个对话正在别处跑着", 409)
    s = sc["claude"].get(sid) if agent == "claude" else next((x for x in history.codex_sessions() if x["id"] == sid), None)
    if not s:
        bad("找不到这个对话的存档", 404)
    rc = bool(d.get("rc")) and agent == "claude"
    if s.get("env"):                                          # 别的机器上的对话：在那台机器上开窗口
        name = await remote.create(s["env"], "agent", s["title"][:60], agent, sid, rc=rc, cwd=s["cwd"])
        remote.invalidate(s["env"])
        db.audit("window", f"resume {name} @{s['env']} {agent}:{sid}{' rc' if rc else ''}{' fork' if at else ''}", st(request)["ip"])
        return {"name": name, "existing": False, "note": ""}
    cwd, note = s["cwd"], ""
    if not cwd or not os.path.isdir(cwd):
        cwd, note = config.DEFAULT_CWD, f"它原来所在的目录 {s['cwd'] or '（未知）'} 已经不在了，改在 {config.DEFAULT_CWD} 里打开"
    name = await tmuxctl.create("agent", cwd, s["title"][:60], agent, sid, rc=rc)
    db.audit("window", f"resume {name} {agent}:{sid}{' rc' if rc else ''}{' fork' if at else ''}", st(request)["ip"])
    return {"name": name, "existing": False, "note": note}


# ---------- 文件 ----------

@api.get("/api/fs/list")
async def fs_list(path: str, hidden: int = 0):
    return fsapi.listdir(path, bool(hidden))


@api.get("/api/fs/read")
async def fs_read(path: str):
    return fsapi.read_text(path)


@api.put("/api/fs/write")
async def fs_write(request: Request):
    d = await body_json(request)
    if not isinstance(d.get("text"), str):
        bad("没有内容")
    r = fsapi.write_text(d.get("path") or "", d["text"], d.get("mtime"), bool(d.get("create")))
    db.audit("fs", f"write {r['path']}", st(request)["ip"])
    return r


@api.post("/api/fs/mkdir")
async def fs_mkdir(request: Request):
    d = await body_json(request)
    fsapi.mkdir(d.get("path") or "")
    db.audit("fs", f"mkdir {d.get('path')}", st(request)["ip"])
    return {"ok": True}


@api.post("/api/fs/rename")
async def fs_rename(request: Request):
    d = await body_json(request)
    fsapi.rename(d.get("src") or "", d.get("dst") or "")
    db.audit("fs", f"rename {d.get('src')} -> {d.get('dst')}", st(request)["ip"])
    return {"ok": True}


@api.post("/api/fs/delete")
async def fs_delete(request: Request):
    d = await body_json(request)
    fsapi.delete(d.get("path") or "", bool(d.get("recursive")))
    db.audit("fs", f"delete {d.get('path')}{' -r' if d.get('recursive') else ''}", st(request)["ip"])
    return {"ok": True}


@api.post("/api/fs/upload")
async def fs_upload(request: Request, file: UploadFile, dir: str, overwrite: int = 0):
    dst = fsapi.upload_target(dir, file.filename or "", bool(overwrite))
    tmp = os.path.join(os.path.dirname(dst), f".term-web-up-{secrets.token_hex(6)}")
    n = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                n += len(chunk)
                if n > config.MAX_UPLOAD:
                    bad("文件太大了", 413)
                out.write(chunk)
        os.replace(tmp, dst)
    except PermissionError:
        bad("没有权限往这个目录里写", 403)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    db.audit("fs", f"upload {dst} {n}B", st(request)["ip"])
    return {"path": dst, "size": n}


@api.get("/api/fs/download")
async def fs_download(path: str, inline: int = 0):
    p, ctype, is_image = fsapi.download_info(path)
    name = urllib.parse.quote(os.path.basename(p))
    show = bool(inline) and is_image
    return FileResponse(p, media_type=ctype if show else "application/octet-stream", headers={
        "Content-Disposition": ("inline" if show else "attachment") + f"; filename*=UTF-8''{name}",
        # 就算浏览器不听 Content-Type 硬要当网页开，也什么脚本都跑不了
        "Content-Security-Policy": "default-src 'none'; sandbox", "Cache-Control": "no-store"})


api.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")

app = Guard(api)
