"""HTTP 层。

安全模型（单用户）：
- 所有请求（页面、静态文件、接口、事件流）都先过 Guard，由 auth.authenticate() 判定；
  不要登录就能访问的只有 PUBLIC 里那几条（登录页本身和它用到的三个静态文件、健康检查）。
- 不用 WebSocket：握手一律拒绝。实时推送走 SSE（普通 GET，带 Cookie，过同一套检查），
  事件流每 30 秒重新校验一次登录。
- 写操作要求自定义请求头 + Origin 同源（防 CSRF）。
"""
import asyncio
import html
import json
import re
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               StreamingResponse)
from starlette.datastructures import Headers, MutableHeaders

from . import auth, backends, config, db, engine, files, mcp, models

BOOT = db.new_id()
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; "
       "object-src 'none'")
CSRF_HEADER = "roundtable"
PUBLIC = {"/healthz", "/login", "/favicon.ico", "/static/login.css", "/static/theme.js", "/static/icon.svg",
          "/auth/password", "/auth/google/start", "/auth/google/callback"}


class Guard:
    """最外层 ASGI 中间件：鉴权、CSRF、安全响应头。WebSocket 一律拒绝。"""

    def __init__(self, app):
        self.app = app

    async def _deny(self, send, status: int, msg: str, location: str = "") -> None:
        body = json.dumps({"error": msg}, ensure_ascii=False).encode()
        hdrs = [(b"content-type", b"application/json; charset=utf-8"), (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"), (b"content-length", str(len(body)).encode())]
        if location:
            hdrs.append((b"location", location.encode()))
        await send({"type": "http.response.start", "status": status, "headers": hdrs})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await receive()
                await send({"type": "websocket.close", "code": 1008})
            return
        path, method = scope["path"], scope["method"]
        headers = Headers(scope=scope)
        if method == "OPTIONS":
            return await self._deny(send, 405, "不支持")
        user, status, why = auth.authenticate(headers, scope.get("client"))
        if path == "/healthz":
            pass
        elif not user and config.AUTH_MODE == "none":
            return await self._deny(send, status, why)          # 不是本机直连：登录页也不给看
        elif not user and path not in PUBLIC:
            # 浏览器直接打开页面 → 去登录页；接口和资源 → 401，前端自己处理
            if method == "GET" and not path.startswith(("/api/", "/static/", "/auth/")):
                return await self._deny(send, 302, why, "/login")
            return await self._deny(send, status, why)
        scope.setdefault("state", {})["user"] = user
        if method not in ("GET", "HEAD"):
            if not auth.origin_ok(headers) or headers.get("sec-fetch-site", "same-origin") != "same-origin":
                got = headers.get("origin") or "（空）"
                return await self._deny(
                    send, 403, f"跨站请求被拒绝。浏览器发来的来源是 {got}，而 ROUNDTABLE_PUBLIC_URL 是 {config.ORIGIN}；"
                               "如果这就是你自己在访问，请把 ROUNDTABLE_PUBLIC_URL 改成浏览器地址栏里的地址。")
            # 登录表单是普通的 HTML 表单，带不了自定义头，只查来源；其它写请求都要带
            if path != "/auth/password" and headers.get("x-requested-with") != CSRF_HEADER:
                return await self._deny(send, 403, "跨站请求被拒绝")

        async def send2(msg):
            if msg["type"] == "http.response.start":
                h = MutableHeaders(scope=msg)
                h["X-Content-Type-Options"] = "nosniff"
                h["Referrer-Policy"] = "same-origin"      # no-referrer 会让浏览器把同站表单的 Origin 也写成 null
                h["X-Frame-Options"] = "DENY"
                h["Cross-Origin-Opener-Policy"] = "same-origin"
                h["Cross-Origin-Resource-Policy"] = "same-origin"
                h["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
                h["Content-Security-Policy"] = CSP
                if config.HTTPS:
                    h["Strict-Transport-Security"] = "max-age=31536000"
                if "cache-control" not in h:
                    h["Cache-Control"] = "no-store"
            await send(msg)
        await self.app(scope, receive, send2)


@asynccontextmanager
async def lifespan(_app):
    for w in config.check():
        print("提醒：" + w, flush=True)
    db.con()
    for d in (config.FILES_DIR, config.RUNS_DIR, config.EMPTY_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if config.AUTH_MODE == "password" and not auth.has_password():
        print("提醒：还没有设密码，现在谁也登录不了。运行  python -m roundtable set-password", flush=True)
    engine.recover()
    await backends.probe()
    await mcp.start_gateway()
    st = backends.status()
    print(f"圆桌 Roundtable：{config.PUBLIC_URL}（监听 {config.HOST}:{config.PORT}，登录方式 {config.AUTH_MODE}）", flush=True)
    for name in ("claude", "codex", "openrouter"):
        print(f"  {name}: " + ("可用 " + st[name].get("version", "") if st[name]["ok"] else "未启用 —— " + st[name]["why"]),
              flush=True)
    asyncio.create_task(models.openrouter_catalog())
    yield
    for t in list(engine.RESP_TASKS.values()):
        t.cancel()
    await asyncio.sleep(0.3)
    await mcp.stop_gateway()


api = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def bad(msg: str, status: int = 400):
    raise HTTPException(status, msg)


@api.exception_handler(HTTPException)
async def _http_exc(_req, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


@api.exception_handler(Exception)
async def _any_exc(_req, exc: Exception):
    print("unhandled:", repr(exc), flush=True)
    return JSONResponse({"error": f"服务器内部错误：{type(exc).__name__}"}, status_code=500)


async def body_json(request: Request) -> dict:
    try:
        d = await request.json()
    except Exception:
        bad("请求体不是合法的 JSON")
    if not isinstance(d, dict):
        bad("请求体格式不对")
    return d


# ---------- 页面与静态文件 ----------

def _ver(name: str) -> str:
    try:
        return str(int((config.STATIC_DIR / name).stat().st_mtime))
    except OSError:
        return "0"


def _page(name: str) -> str:
    """读一个页面模板，给里面的静态文件地址加上版本号（改了文件浏览器就会重新取）。"""
    page = (config.STATIC_DIR / name).read_text(encoding="utf-8")
    return re.sub(r'((?:href|src)=")/static/([^"?]+)"',
                  lambda m: f'{m.group(1)}/static/{m.group(2)}?v={_ver(m.group(2))}"', page)


@api.get("/")
async def index():
    return HTMLResponse(_page("index.html"), headers={"Cache-Control": "no-store"})


@api.get("/static/{path:path}")
async def static(path: str):
    root = config.STATIC_DIR.resolve()
    p = (root / path).resolve()
    if not p.is_file() or root not in p.parents:
        bad("not found", 404)
    cache = "private, max-age=31536000, immutable" if path.startswith("vendor/") else "no-cache"
    return FileResponse(p, headers={"Cache-Control": cache})


@api.get("/favicon.ico")
async def favicon():
    return FileResponse(config.STATIC_DIR / "icon.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "private, max-age=86400"})


@api.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")


# ---------- 登录 ----------

LOGIN_MSG = {
    "badpw": "密码不对。",
    "locked": "密码连续输错太多次，登录暂时锁住了，请过 {wait} 再试。",
    "nopw": "服务器上还没有设密码。请在服务器上运行 python -m roundtable set-password。",
    "state": "这次登录已经过期或者不是从这个浏览器发起的，请重新点一次登录。",
    "denied": "这个 Google 账号不在允许名单里。",
    "unverified": "这个账号的邮箱没有经过验证，不能用来登录。",
    "provider": "和登录服务通信时出了问题，请稍后再试；一直不行就看服务器日志。",
    "cancel": "登录被取消了。",
    "bye": "已退出登录。",
}


def _wait_text(sec: int) -> str:
    return f"{sec} 秒" if sec < 90 else f"{(sec + 59) // 60} 分钟"


def _login_page(code: str = "") -> HTMLResponse:
    msg = LOGIN_MSG.get(code, "")
    if code == "locked":
        left = auth.lock_remaining()
        msg = msg.format(wait=_wait_text(left)) if left else ""          # 锁已经过期了就不用再提
    note = f'<p class="note{" ok" if code == "bye" else ""}" role="alert">{html.escape(msg)}</p>' if msg else ""
    if config.AUTH_MODE == "google":
        form = '<a class="btn primary" href="/auth/google/start">用 Google 账号登录</a>'
        hint = "只有部署者指定的账号能进来。"
    else:
        form = ('<form method="post" action="/auth/password">'
                '<label for="pw">密码</label>'
                '<input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>'
                '<button class="btn primary" type="submit">登录</button></form>')
        hint = "这是一个私人部署，只有部署者自己能进来。"
    page = _page("login.html").replace("{{note}}", note).replace("{{form}}", form).replace("{{hint}}", hint)
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


def _go(url: str, cookies: list[str] = ()) -> RedirectResponse:
    r = RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})
    for c in cookies:
        r.headers.append("Set-Cookie", c)
    return r


@api.get("/login")
async def login_page(request: Request, e: str = ""):
    if request.scope["state"].get("user"):
        return _go("/")
    return _login_page(e if e in LOGIN_MSG else "")


@api.post("/auth/password")
async def auth_password(request: Request):
    if config.AUTH_MODE != "password":
        bad("not found", 404)
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > 8192:
            bad("请求太大", 413)
    pw = (urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).get("password") or [""])[0]
    try:
        token = await auth.login_password(pw)
    except auth.AuthError as e:
        return _go("/login?e=" + e.code)
    return _go("/", [auth.set_cookie(auth.COOKIE, token, config.SESSION_DAYS * 86400)])


@api.get("/auth/google/start")
async def auth_google_start():
    if config.AUTH_MODE != "google":
        bad("not found", 404)
    try:
        url, state = await auth.oidc_start()
    except auth.AuthError as e:
        print("auth:", e, flush=True)
        return _go("/login?e=" + e.code)
    return _go(url, [auth.set_cookie(auth.FLOW_COOKIE, state, auth.FLOW_TTL)])


@api.get("/auth/google/callback")
async def auth_google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
    if config.AUTH_MODE != "google":
        bad("not found", 404)
    gone = [auth.clear_cookie(auth.FLOW_COOKIE)]
    if error:
        return _go("/login?e=" + ("cancel" if error == "access_denied" else "provider"), gone)
    try:
        token = await auth.oidc_finish(state, auth.cookie_value(request.headers.get("cookie"), auth.FLOW_COOKIE), code)
    except auth.AuthError as e:
        print("auth:", e, flush=True)
        return _go("/login?e=" + e.code, gone)
    return _go("/", gone + [auth.set_cookie(auth.COOKIE, token, config.SESSION_DAYS * 86400)])


@api.post("/auth/logout")
async def auth_logout(request: Request):
    auth.destroy_session(auth.cookie_value(request.headers.get("cookie"), auth.COOKIE))
    r = JSONResponse({"ok": True, "next": "/login?e=bye"})
    r.headers.append("Set-Cookie", auth.clear_cookie(auth.COOKIE))
    return r


# ---------- 基本信息 ----------

SETTING_KEYS = {"custom_instructions": str, "synth_instructions": str, "layout": str, "or_web": bool, "or_favorites": list, "presets": list,
                "send_key": str}


# OpenRouter 上默认收藏的几个（清单里没有的会自动略过，过时了也不碍事）
DEFAULT_FAVORITES = ["google/gemini-4-argon", "x-ai/grok-4.7", "google/gemini-3.8-flash", "deepseek/deepseek-v4.1-flash",
                     "google/gemini-3.1-flash-image", "openai/gpt-5.4-image-2"]


def default_presets(cat: dict, favorites: list[str]) -> list[dict]:
    """按实际能用的后端生成几个起步的预设。"""
    def pick(lst, prefer):
        return next((m["key"] for m in lst if m["id"] == prefer), lst[0]["key"] if lst else None)
    have = {m["key"]: m for m in cat["openrouter"]}
    picks = [(pick(cat["claude"], "opus"), "Claude"), (pick(cat["codex"], ""), "GPT"),
             (next((k for k in favorites if k in have and not have[k].get("image_out")), None), "OpenRouter")]
    picks = [(k, n) for k, n in picks if k]
    out = [{"name": models.label_sync(k), "models": [{"key": k}], "mode": "single"} for k, _ in picks[:1]]
    if len(picks) >= 2:
        two = picks[:2]
        out.append({"name": " × ".join(n for _, n in two) + " 互评", "models": [{"key": k} for k, _ in two],
                    "mode": "review", "rounds": 1, "synth": two[0][0]})
    if len(picks) >= 3:
        out.append({"name": "三方会诊", "models": [{"key": k} for k, _ in picks], "mode": "review", "rounds": 1,
                    "synth": picks[0][0]})
        out.append({"name": "三家各答各的", "models": [{"key": k} for k, _ in picks], "mode": "parallel"})
    return out


async def ensure_defaults() -> None:
    cat = await models.catalog()
    if db.get_setting("or_favorites") is None and cat["openrouter"]:
        have = {m["id"] for m in cat["openrouter"]}
        db.set_setting("or_favorites", [f"or:{i}" for i in DEFAULT_FAVORITES if i in have])
    if db.get_setting("presets") is None and (cat["claude"] or cat["codex"] or cat["openrouter"]):
        db.set_setting("presets", default_presets(cat, db.get_setting("or_favorites", [])))


def settings_public() -> dict:
    return {"custom_instructions": db.get_setting("custom_instructions", ""),
            "synth_instructions": db.get_setting("synth_instructions", ""),
            "synth_default": engine.synth_default_text(),
            "layout": db.get_setting("layout", "columns"),
            "or_web": bool(db.get_setting("or_web", False)),
            "or_favorites": db.get_setting("or_favorites", []) or [],
            "presets": db.get_setting("presets", []) or [],
            "send_key": db.get_setting("send_key", "enter"),
            "last_config": db.get_setting("last_config", None)}


@api.get("/api/me")
async def me(request: Request):
    await ensure_defaults()
    return {"user": request.scope["state"]["user"], "auth_mode": config.AUTH_MODE, "boot": BOOT,
            "settings": settings_public(), "session_days": config.SESSION_DAYS, "mcp": mcp.summary(),
            "limits": {"max_models": config.MAX_MODELS_PER_TURN, "max_upload": config.MAX_UPLOAD}}


@api.get("/api/models")
async def get_models(refresh: int = 0):
    if refresh:
        await models.openrouter_catalog(force=True)
    await ensure_defaults()
    cat = await models.catalog()
    cat["openrouter_ready"] = bool(config.openrouter_key())
    return cat


@api.get("/api/settings")
async def get_settings():
    return settings_public()


@api.put("/api/settings")
async def put_settings(request: Request):
    d = await body_json(request)
    for k, v in d.items():
        if k not in SETTING_KEYS or not isinstance(v, SETTING_KEYS[k]):
            continue
        if k == "custom_instructions":
            v = v[:8000]
        if k == "synth_instructions":
            v = v.strip()[:engine.MAX_SYNTH_INSTR]
        if k in ("or_favorites", "presets") and len(json.dumps(v)) > 100_000:
            continue
        db.set_setting(k, v)
    return settings_public()


_usage_cache: dict = {"at": 0.0, "or": None}


@api.get("/api/usage")
async def usage():
    if time.time() - _usage_cache["at"] > 60:
        _usage_cache.update(at=time.time(), **{"or": await backends.openrouter_key_info()})
    since = time.time() - 30 * 86400
    rows = db.q("SELECT model, COUNT(*) AS n, SUM(COALESCE(json_extract(usage,'$.cost'),0)) AS cost, "
                "SUM(COALESCE(json_extract(usage,'$.cost_est'),0)) AS cost_est, "
                "SUM(COALESCE(json_extract(usage,'$.in'),0)) AS tin, SUM(COALESCE(json_extract(usage,'$.out'),0)) AS tout "
                "FROM responses WHERE started_at>? AND status IN ('done','stopped','error') GROUP BY model "
                "ORDER BY n DESC", (since,))
    return {"openrouter": _usage_cache["or"], "backends": backends.status(),
            "claude_rate_limit": backends.STATE.get("claude_rate_limit"),
            "by_model": [{"model": r["model"], "label": models.label_sync(r["model"]), "n": r["n"],
                          "cost": r["cost"], "cost_est": r["cost_est"], "in": r["tin"], "out": r["tout"]}
                         for r in rows]}


# ---------- 对话 ----------

@api.get("/api/conversations")
async def list_convs(q: str = ""):
    q = q.strip()
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = db.q(
            "SELECT * FROM conversations c WHERE c.title LIKE ? ESCAPE '\\' "
            "OR EXISTS(SELECT 1 FROM turns t WHERE t.conv_id=c.id AND t.user_text LIKE ? ESCAPE '\\') "
            "OR EXISTS(SELECT 1 FROM responses r WHERE r.conv_id=c.id AND r.text LIKE ? ESCAPE '\\') "
            "ORDER BY c.updated_at DESC LIMIT 200", (like, like, like))
    else:
        rows = db.q("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 2000")
    return {"conversations": [engine.conv_summary(db.row(r)) for r in rows]}


@api.get("/api/conversations/{cid}")
async def get_conv(cid: str):
    c = engine.conv_full(cid)
    if not c:
        bad("对话不存在", 404)
    return c


@api.patch("/api/conversations/{cid}")
async def patch_conv(cid: str, request: Request):
    d = await body_json(request)
    if not db.q1("SELECT 1 FROM conversations WHERE id=?", (cid,)):
        bad("对话不存在", 404)
    if isinstance(d.get("title"), str) and d["title"].strip():
        db.x("UPDATE conversations SET title=?, title_auto=0 WHERE id=?", (d["title"].strip()[:120], cid))
    if "pinned" in d:
        db.x("UPDATE conversations SET pinned=? WHERE id=?", (1 if d["pinned"] else 0, cid))
    engine.publish_conv(cid)
    return {"ok": True}


@api.delete("/api/conversations/{cid}")
async def delete_conv(cid: str):
    if not db.q1("SELECT 1 FROM conversations WHERE id=?", (cid,)):
        bad("对话不存在", 404)
    engine.delete_conversation(cid)
    return {"ok": True}


STAGE_NAME = {"answer": "回答", "review": "互评与修订", "synth": "综合"}


@api.get("/api/conversations/{cid}/export")
async def export_conv(cid: str):
    c = engine.conv_full(cid)
    if not c:
        bad("对话不存在", 404)
    out = [f"# {c['title']}", "",
           f"> 导出时间：{time.strftime('%Y-%m-%d %H:%M')}　对话创建：{time.strftime('%Y-%m-%d %H:%M', time.localtime(c['created_at']))}", ""]
    for t in c["turns"]:
        out += [f"## 第 {t['seq']} 轮 · 我", "", t["user_text"] or "", ""]
        if t["attachments"]:
            out += ["附件：" + "、".join(a["name"] for a in t["attachments"]), ""]
        multi = len({r["model"] for r in t["responses"]}) > 1
        for r in t["responses"]:
            if r["status"] in ("pending", "skipped") or not (r["text"] or r["images"]):
                continue
            head = f"### {r['label']}"
            if multi or r["stage"] != "answer":
                head += f" · {STAGE_NAME.get(r['stage'], r['stage'])}" + (f"（第 {r['round']} 轮）" if r["stage"] == "review" else "")
            out += [head, ""]
            if r["stage"] == "synth" and r.get("instr"):
                out += ["> 综合要求：" + r["instr"].replace("\n", "\n> "), ""]
            out += [r["text"] or "", ""]
            if r["images"]:
                out += ["（生成图片：" + "、".join(i["name"] for i in r["images"]) + "）", ""]
    name = re.sub(r'[\\/:*?"<>|\r\n]+', " ", c["title"]).strip()[:60] or "对话"
    return PlainTextResponse("\n".join(out), media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(name + ".md")})


# ---------- 发消息 / 追加 / 停止 ----------

@api.post("/api/turns")
async def post_turn(request: Request):
    d = await body_json(request)
    text = d.get("text") or ""
    if not isinstance(text, str) or len(text) > 2_000_000:
        bad("消息太长")
    att = [a for a in (d.get("attachments") or []) if isinstance(a, str)][:40]
    try:
        return await engine.create_turn(d.get("conv_id") or None, text, att, d.get("models") or [],
                                        d.get("mode") or "parallel", d.get("rounds") or 1, d.get("synth"),
                                        bool(d.get("web")), d.get("replace_from") or None)
    except ValueError as e:
        bad(str(e))


@api.post("/api/turns/{tid}/extend")
async def post_extend(tid: str, request: Request):
    d = await body_json(request)
    try:
        await engine.extend_turn(tid, d.get("action") or "", d.get("models") or [])
    except ValueError as e:
        bad(str(e))
    return {"ok": True}


@api.post("/api/turns/{tid}/stop")
async def post_stop_turn(tid: str):
    engine.stop_turn(tid)
    return {"ok": True}


@api.post("/api/responses/{rid}/stop")
async def post_stop_resp(rid: str):
    engine.stop_response(rid)
    return {"ok": True}


@api.post("/api/responses/{rid}/regenerate")
async def post_regen(rid: str):
    try:
        engine.regenerate(rid)
    except ValueError as e:
        bad(str(e))
    return {"ok": True}


# ---------- 文件 ----------

@api.post("/api/files")
async def upload(file: UploadFile):
    data = await file.read(config.MAX_UPLOAD + 1)
    if len(data) > config.MAX_UPLOAD:
        bad(f"文件超过 {config.MAX_UPLOAD // (1024 * 1024)}MB", 413)
    try:
        rec = await asyncio.to_thread(files.save_upload, file.filename or "file", data, file.content_type or "")
    except files.FileError as e:
        bad(str(e))
    return files.insert_file(rec, None)


INLINE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"}


@api.get("/api/files/{fid}")
async def get_file(fid: str, dl: int = 0):
    f = files.get(fid)
    if not f or not Path(f["path"]).is_file():
        bad("文件不存在", 404)
    inline = f["mime"] in INLINE_MIMES and not dl
    mime = f["mime"] if f["mime"] in INLINE_MIMES else "application/octet-stream"
    disp = ("inline" if inline else "attachment") + "; filename*=UTF-8''" + urllib.parse.quote(f["name"])
    return FileResponse(f["path"], media_type=mime, headers={
        "Content-Disposition": disp, "Cache-Control": "private, max-age=31536000, immutable",
        "Content-Security-Policy": "default-src 'none'; sandbox" if f["mime"] != "application/pdf" else CSP})


@api.delete("/api/files/{fid}")
async def del_file(fid: str):
    f = files.get(fid)
    if f and f["conv_id"] is None:
        files.delete_file_rows([f])
    return {"ok": True}


# ---------- MCP（设置里接的远程工具服务器） ----------

@api.get("/api/mcp/servers")
async def mcp_list():
    return {**mcp.state(), "flash": mcp.pop_flash()}


@api.post("/api/mcp/servers")
async def mcp_add(request: Request):
    d = await body_json(request)
    f = {k: d.get(k) if isinstance(d.get(k), str) else "" for k in ("name", "url", "token", "header", "client_id",
                                                                    "client_secret")}
    try:
        s = await mcp.add_server(**f)
    except ValueError as e:
        bad(str(e))
    return {**mcp.state(), "id": s["id"]}


@api.patch("/api/mcp/servers/{sid}")
async def mcp_patch(sid: str, request: Request):
    try:
        mcp.patch_server(sid, await body_json(request))
    except ValueError as e:
        bad(str(e), 404)
    return mcp.state()


@api.post("/api/mcp/servers/{sid}/refresh")
async def mcp_refresh(sid: str):
    try:
        await mcp.refresh_server(sid)
    except ValueError as e:
        return {**mcp.state(), "error": str(e)}
    return mcp.state()


@api.post("/api/mcp/servers/{sid}/oauth/start")
async def mcp_oauth_start(sid: str):
    try:
        return {"url": mcp.oauth_start(sid)}
    except ValueError as e:
        bad(str(e))


@api.get("/api/mcp/oauth/callback")
async def mcp_oauth_callback(request: Request):
    # 授权服务器把浏览器送回这里（顶层 GET 跳转，带着登录 Cookie，照样先过 Guard）。state 只能用一次，
    # 而且只认本应用刚刚发起、还没过期的那一个，所以别人伪造的回调换不到任何东西。
    await mcp.oauth_callback({k: v for k, v in request.query_params.items() if len(v) < 4000})
    return RedirectResponse("/#/settings/mcp", status_code=303)


@api.delete("/api/mcp/servers/{sid}")
async def mcp_delete(sid: str):
    await mcp.delete_server(sid)
    return mcp.state()


# ---------- 事件流（SSE） ----------

@api.get("/api/events")
async def events(request: Request):
    hdrs, client = request.headers, request.scope.get("client")
    q = engine.subscribe()

    async def gen():
        last_auth = time.time()
        try:
            yield "retry: 2000\n\n"
            yield "data: " + json.dumps({"type": "hello", "boot": BOOT}) + "\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    buf = ["data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"]
                    while not q.empty() and len(buf) < 200:
                        buf.append("data: " + json.dumps(q.get_nowait(), ensure_ascii=False) + "\n\n")
                    yield "".join(buf)
                except TimeoutError:
                    yield ": ping\n\n"
                if time.time() - last_auth > 30:
                    last_auth = time.time()
                    if not auth.authenticate(hdrs, client)[0]:
                        yield "data: " + json.dumps({"type": "auth"}) + "\n\n"
                        return
                if q not in engine._subs:           # 队列积压被踢掉了，让客户端重连重新同步
                    return
        finally:
            engine.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})


app = Guard(api)
