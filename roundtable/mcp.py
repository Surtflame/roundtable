"""MCP：在网页「设置 → MCP」里接的远程 MCP 服务器。默认一个都没有。

结构
- 本应用是上游 MCP 服务器唯一的客户端。令牌、OAuth 的刷新令牌只存在这里（数据目录里的 roundtable.db），
  不交给任何模型，也不交给 claude / codex 子进程。
- claude / codex 子进程连的是本机网关（127.0.0.1:MCP_GATEWAY_PORT/mcp/<slug>），带一个只在这次回答里有效的
  运行令牌；网关只给出用户勾选过的工具，再替它们去调上游。OpenRouter 的模型没有子进程，后端直接调 RunCtx.call()。
- 所以三种后端走同一条路：同一份白名单、同一处记录调用过程、同一个结果长度上限。
- 只支持远程的 Streamable HTTP 服务器。不支持本机命令（stdio），也不支持旧的 HTTP+SSE 传输。
"""
import asyncio
import base64
import contextlib
import hashlib
import json
import re
import secrets
import socket
import time
import urllib.parse
from collections import defaultdict

import httpx
import uvicorn

from . import config, db

PROTO = "2025-06-18"
PROTOS = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
RESP_CAP = 8_000_000              # 上游一次回应最多读多少字节
BODY_CAP = 4_000_000              # 网关一次请求体最多多少字节
MAX_TOOLS = 200
BAD_HEADERS = {"host", "content-length", "content-type", "accept", "connection", "transfer-encoding",
               "mcp-session-id", "mcp-protocol-version", "cookie"}


class McpError(Exception):
    """给用户看的中文错误。不要把地址写进去（地址里可能带密钥）。"""


class McpAuth(McpError):
    """上游返回 401。www 是它的 WWW-Authenticate 头。"""

    def __init__(self, www: str = "", msg: str = "服务器要求登录（401）"):
        super().__init__(msg)
        self.www = www


class OAuthFail(Exception):
    def __init__(self, msg: str, fatal: bool = False):
        super().__init__(msg)
        self.fatal = fatal        # 4xx：重试也没用，得重新授权


# ---------- 存取 ----------

_JSON = ("secret", "tools", "info")


def _dec(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in _JSON:
        try:
            d[k] = json.loads(d[k]) if d[k] else ([] if k == "tools" else {})
        except Exception:
            d[k] = [] if k == "tools" else {}
    return d


def servers() -> list[dict]:
    return [_dec(r) for r in db.q("SELECT * FROM mcp_servers ORDER BY created_at")]


def get(sid: str) -> dict | None:
    return _dec(db.q1("SELECT * FROM mcp_servers WHERE id=?", (sid,)))


def _set(sid: str, **kw) -> None:
    if not kw:
        return
    kw["updated_at"] = db.now()
    cols = ", ".join(f"{k}=?" for k in kw)
    vals = [json.dumps(v, ensure_ascii=False) if k in _JSON else v for k, v in kw.items()]
    db.x(f"UPDATE mcp_servers SET {cols} WHERE id=?", (*vals, sid))


def _insert(s: dict) -> None:
    db.x("INSERT INTO mcp_servers(id,slug,name,url,enabled,auth,secret,tools,info,status,error,created_at,updated_at) "
         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
         (s["id"], s["slug"], s["name"], s["url"], 1, s["auth"], json.dumps(s["secret"], ensure_ascii=False),
          json.dumps(s["tools"], ensure_ascii=False), json.dumps(s["info"], ensure_ascii=False), s["status"],
          s.get("error"), db.now(), db.now()))


def _clip(s, n: int) -> str:
    s = s if isinstance(s, str) else str(s or "")
    return s if len(s) <= n else s[:n] + "…"


def _shown(url: str) -> str:
    """给界面看的地址：太长的路径段（多半是密钥）和查询串不显示。"""
    u = urllib.parse.urlsplit(url)
    segs = ["…" if len(p) >= 20 else p for p in u.path.split("/")]
    return f"{u.scheme}://{u.netloc}{'/'.join(segs)}{'?…' if u.query else ''}"


def public(s: dict) -> dict:
    info = s["info"] if isinstance(s["info"], dict) else {}
    return {"id": s["id"], "slug": s["slug"], "name": s["name"], "url": _shown(s["url"]),
            "host": urllib.parse.urlsplit(s["url"]).hostname or "", "enabled": bool(s["enabled"]), "auth": s["auth"],
            "status": s["status"], "error": s["error"],
            "server": {"name": _clip(info.get("name"), 80), "version": _clip(info.get("version"), 40)},
            "tools": [{"name": t["name"], "title": t.get("title") or "", "description": t.get("description") or "",
                       "params": list((t.get("inputSchema") or {}).get("properties") or {})[:40],
                       "ro": bool(t.get("ro")), "enabled": bool(t.get("enabled")), "changed": bool(t.get("changed"))}
                      for t in s["tools"]]}


def _live(s: dict) -> bool:
    return bool(s["enabled"]) and s["status"] == "ok"


def summary() -> dict:
    """现在实际会带给模型的东西，给输入框旁边的小标记用。"""
    on, idle, attention = [], [], []
    for s in servers():
        if not s["enabled"]:
            continue
        n = sum(1 for t in s["tools"] if t.get("enabled"))
        if s["status"] != "ok":
            attention.append(s["name"])
        elif n:
            on.append({"name": s["name"], "tools": n})
        else:
            idle.append(s["name"])
    return {"on": on, "tools": sum(x["tools"] for x in on), "idle": idle, "attention": attention,
            "gateway": GW["port"] is not None}


def state() -> dict:
    return {"servers": [public(s) for s in servers()], "summary": summary()}


FLASH: list[dict] = []


def flash(text: str, ok: bool = False, sid: str = "") -> None:
    FLASH.append({"text": text, "ok": ok, "sid": sid})
    del FLASH[:-5]


def pop_flash() -> list[dict]:
    out = list(FLASH)
    FLASH.clear()
    return out


# ---------- 地址检查、写死的拒绝 ----------

def _loopback(host: str | None) -> bool:
    return bool(host) and (host in ("localhost", "::1") or host.startswith("127."))


def _origin(url: str) -> tuple:
    u = urllib.parse.urlsplit(url)
    return u.scheme, (u.hostname or "").lower(), u.port or (443 if u.scheme == "https" else 80)


def check_url(url: str) -> str:
    url = (url or "").strip()
    if not url or len(url) > 2000 or re.search(r"\s", url):
        raise ValueError("地址不对")
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        raise ValueError("地址不对") from None
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host:
        raise ValueError("地址要以 https:// 开头")
    if u.username or u.password:
        raise ValueError("地址里不要带用户名和密码")
    if u.scheme == "http" and not _loopback(host):
        raise ValueError("只支持 https 地址")
    if _origin(url) == _origin(config.ORIGIN) or (_loopback(host) and port in (config.PORT, GW["port"])):
        raise ValueError("不能把圆桌自己接成 MCP")
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, u.query, ""))


def _ok_endpoint(url, loop_ok: bool) -> bool:
    """授权服务器给的地址：必须是 https；只有 MCP 服务器本身在本机时才允许本机的 http。"""
    if not isinstance(url, str) or len(url) > 2000:
        return False
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if not u.hostname or u.username or u.password:
        return False
    return u.scheme == "https" or (u.scheme == "http" and loop_ok and _loopback(u.hostname))


# ---------- 上游：Streamable HTTP 客户端 ----------

_http: httpx.AsyncClient | None = None
_SESS: dict[str, dict] = {}                       # 服务器 id -> {sid, proto, info}
_ILOCK: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_RLOCK: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_seq = 0


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(follow_redirects=False, trust_env=False, limits=httpx.Limits(max_connections=40),
                                  timeout=httpx.Timeout(connect=config.MCP_CONNECT_TIMEOUT, write=60, pool=20,
                                                        read=config.MCP_CALL_TIMEOUT + 30))
    return _http


def _next_id() -> int:
    global _seq
    _seq += 1
    return _seq


async def _read_cap(r: httpx.Response, cap: int) -> bytes:
    buf = bytearray()
    async for chunk in r.aiter_bytes():
        buf += chunk
        if len(buf) > cap:
            raise McpError("对方返回的内容太大")
    return bytes(buf)


async def _small(method: str, url: str, **kw) -> tuple[int, httpx.Headers, bytes]:
    """发现文档、注册、换令牌这类小请求。"""
    try:
        async with asyncio.timeout(config.MCP_CONNECT_TIMEOUT):
            async with http().stream(method, url, **kw) as r:
                return r.status_code, r.headers, await _read_cap(r, 256_000)
    except TimeoutError:
        raise McpError("对方太久没有回应") from None
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise McpError(f"连不上（{type(e).__name__}）") from None


async def _get_json(url: str) -> dict | None:
    try:
        status, _h, raw = await _small("GET", url, headers={"Accept": "application/json"})
        d = json.loads(raw) if status == 200 else None
    except (McpError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _auth_headers(s: dict) -> dict:
    sec = s["secret"]
    if s["auth"] == "header" and sec.get("value"):
        return {sec.get("header") or "Authorization": sec["value"]}
    if s["auth"] == "oauth" and sec.get("access"):
        return {"Authorization": "Bearer " + sec["access"]}
    return {}


def _pick(data, want_id):
    for m in data if isinstance(data, list) else [data]:
        if isinstance(m, dict) and m.get("id") == want_id and ("result" in m or "error" in m):
            return m
    return None


async def _read_sse(r: httpx.Response, want_id):
    data, total = [], 0
    async for line in r.aiter_lines():
        total += len(line) + 1
        if total > RESP_CAP:
            raise McpError("对方返回的内容太大")
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif line == "" and data:
            try:
                got = _pick(json.loads("\n".join(data)), want_id)
            except ValueError:
                got = None
            data = []
            if got:
                return got
    if data:
        try:
            return _pick(json.loads("\n".join(data)), want_id)
        except ValueError:
            pass
    return None


async def _send(s: dict, msg: dict, *, timeout: float, want_id=None, st: dict | None = None):
    """发一条 JSON-RPC。返回 (HTTP 状态, 响应头, 对应的回应或 None)。"""
    hdrs = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **_auth_headers(s)}
    if st:
        if st.get("sid"):
            hdrs["Mcp-Session-Id"] = st["sid"]
        hdrs["MCP-Protocol-Version"] = st["proto"]
    try:
        async with asyncio.timeout(timeout):
            async with http().stream("POST", s["url"], content=json.dumps(msg, ensure_ascii=False).encode(),
                                     headers=hdrs) as r:
                if r.status_code >= 300 or want_id is None:
                    return r.status_code, r.headers, None
                if r.headers.get("content-type", "").split(";")[0].strip().lower() == "text/event-stream":
                    return r.status_code, r.headers, await _read_sse(r, want_id)
                raw = await _read_cap(r, RESP_CAP)
                try:
                    return r.status_code, r.headers, _pick(json.loads(raw), want_id)
                except ValueError:
                    raise McpError("服务器返回的不是 JSON，这个地址可能不是 MCP 端点") from None
    except TimeoutError:
        raise McpError(f"等了 {int(timeout)} 秒服务器还没回应") from None
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise McpError(f"连不上服务器（{type(e).__name__}）") from None


async def _init(s: dict, stale: dict | None = None) -> dict:
    """握手。同一个服务器的握手结果在内存里留着，会话失效（stale）时重来一次。"""
    async with _ILOCK[s["id"]]:
        cur = _SESS.get(s["id"])
        if cur and cur is not stale:
            return cur
        _SESS.pop(s["id"], None)
        rid = _next_id()
        status, hdrs, reply = await _send(s, {"jsonrpc": "2.0", "id": rid, "method": "initialize", "params": {
            "protocolVersion": PROTO, "capabilities": {}, "clientInfo": {"name": "roundtable", "version": "1"}}},
            timeout=config.MCP_CONNECT_TIMEOUT, want_id=rid)
        if status == 401:
            raise McpAuth(hdrs.get("www-authenticate") or "")
        if status in (404, 405):
            raise McpError(f"这个地址不是 Streamable HTTP 的 MCP 端点（返回 {status}）。旧的 SSE 传输方式不支持。")
        if 300 <= status < 400:
            raise McpError("服务器返回了跳转。请填跳转之后的最终地址。")
        if status == 403:
            raise McpError("服务器拒绝了请求（403）")
        if status >= 400:
            raise McpError(f"服务器返回 HTTP {status}")
        if not reply:
            raise McpError("握手没有得到回应，这个地址可能不是 MCP 端点")
        if "error" in reply:
            raise McpError("握手被拒绝：" + _clip((reply["error"] or {}).get("message"), 200))
        res = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        proto = res.get("protocolVersion")
        sid = hdrs.get("mcp-session-id")
        st = {"sid": sid if sid and re.fullmatch(r"[\x21-\x7e]{1,400}", sid) else None,
              "proto": proto if isinstance(proto, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", proto) else PROTO,
              "info": res.get("serverInfo") if isinstance(res.get("serverInfo"), dict) else {}}
        with contextlib.suppress(McpError):
            await _send(s, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        timeout=config.MCP_CONNECT_TIMEOUT, st=st)
        _SESS[s["id"]] = st
        return st


def _mark_auth(s: dict) -> None:
    if not db.q1("SELECT 1 FROM mcp_servers WHERE id=?", (s["id"],)):
        return
    if s["auth"] == "oauth":
        _set(s["id"], status="needs_auth", error="授权失效了，需要重新授权")
    elif s["auth"] == "header":
        _set(s["id"], status="error", error="服务器不认这个令牌了（401）")
    else:
        _set(s["id"], status="error", error="服务器现在要求登录（401）。删掉重新加一次。")


async def rpc(s: dict, method: str, params: dict | None = None, *, timeout: float | None = None) -> dict:
    """调上游的一个方法，返回 result。自动握手；401 时刷新一次令牌；会话过期时重新握手一次。"""
    timeout = timeout or config.MCP_CONNECT_TIMEOUT
    refreshed = reinit = False
    while True:
        try:
            await _fresh(s)
            st = _SESS.get(s["id"]) or await _init(s)
            rid = _next_id()
            status, hdrs, reply = await _send(s, {"jsonrpc": "2.0", "id": rid, "method": method,
                                                  "params": params or {}}, timeout=timeout, want_id=rid, st=st)
            if status == 401:
                raise McpAuth(hdrs.get("www-authenticate") or "")
        except McpAuth:
            if s["auth"] == "oauth" and not refreshed and s["secret"].get("refresh"):
                refreshed = True
                _SESS.pop(s["id"], None)
                await _refresh(s)
                continue
            _mark_auth(s)
            raise
        if status in (400, 404) and st.get("sid") and not reinit:   # 多半是会话过期了
            reinit = True
            await _init(s, stale=st)
            continue
        if status == 403:
            raise McpError("服务器拒绝了这次请求（403，可能是授权的权限范围不够）")
        if status >= 300:
            raise McpError(f"服务器返回 HTTP {status}")
        if not reply:
            raise McpError("服务器没给结果就断开了")
        if "error" in reply:
            err = reply["error"] if isinstance(reply["error"], dict) else {}
            raise McpError("服务器报错：" + _clip(err.get("message") or "未知错误", 300))
        return reply["result"] if isinstance(reply.get("result"), dict) else {}


async def list_tools(s: dict) -> list[dict]:
    out, cursor = [], None
    for _ in range(20):
        res = await rpc(s, "tools/list", {"cursor": cursor} if cursor else {})
        out += [t for t in res.get("tools") or [] if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"]]
        cursor = res.get("nextCursor")
        if not isinstance(cursor, str) or not cursor or len(out) >= MAX_TOOLS:
            break
    return out[:MAX_TOOLS]


def _xname(name: str, used: set) -> str:
    """给模型看的工具名：只留字母数字下划线（各家对工具名的要求不一样，取交集）。"""
    base = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", name)).strip("_")[:44] or "tool"
    x, i = base, 2
    while x in used:
        x = f"{base[:40]}_{i}"
        i += 1
    used.add(x)
    return x


def _merge_tools(old: list[dict], fresh: list[dict]) -> list[dict]:
    """上游的工具表并进已有的勾选。新工具不勾；说明或参数变过的，取消勾选并标出来；没了的去掉。"""
    oldm = {t["name"]: t for t in old}
    used, seen, out = set(), set(), []
    for t in fresh:
        name = t["name"][:200]
        if name in seen:
            continue
        seen.add(name)
        desc = _clip(t.get("description"), 4000)
        schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {"type": "object", "properties": {}}
        if len(json.dumps(schema)) > 30_000:
            schema = {"type": "object", "additionalProperties": True}
        schema = {k: v for k, v in schema.items() if k != "$schema"}
        schema.setdefault("type", "object")
        ann = t.get("annotations") if isinstance(t.get("annotations"), dict) else {}
        hsh = hashlib.sha256(json.dumps([desc, schema], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        o = oldm.get(name)
        same = bool(o) and o.get("hash") == hsh
        out.append({"name": name, "x": o["x"] if o and o.get("x") and o["x"] not in used else None,
                    "title": _clip(ann.get("title") or t.get("title") or "", 80), "description": desc,
                    "inputSchema": schema, "ro": ann.get("readOnlyHint") is True, "hash": hsh,
                    "enabled": bool(same and o.get("enabled")),
                    "changed": bool(o and ((not same and o.get("enabled")) or (o.get("changed") and not o.get("enabled"))))})
        if out[-1]["x"]:
            used.add(out[-1]["x"])
    for t in out:
        if not t["x"]:
            t["x"] = _xname(t["name"], used)
    return out


# ---------- OAuth（授权码 + PKCE，动态注册） ----------

PENDING: dict[str, dict] = {}                     # state -> {sid, verifier, at}；只在内存里，10 分钟过期


def redirect_uri() -> str:
    return config.ORIGIN + "/api/mcp/oauth/callback"


def _www_params(h: str) -> dict:
    return {m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
            for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^\s,]+))', h or "")}


async def _discover(url: str, www: str) -> dict:
    """从 401 开始找授权服务器：受保护资源元数据 → 授权服务器元数据。"""
    u = urllib.parse.urlsplit(url)
    origin, path, loop = f"{u.scheme}://{u.netloc}", u.path.rstrip("/"), _loopback(u.hostname)
    p = _www_params(www)
    cands = [p.get("resource_metadata"), origin + "/.well-known/oauth-protected-resource" + path,
             origin + "/.well-known/oauth-protected-resource"]
    prm = {}
    for c in dict.fromkeys(c for c in cands if _ok_endpoint(c, loop)):
        prm = await _get_json(c) or {}
        if prm.get("authorization_servers") or prm.get("resource"):
            break
    resource = prm.get("resource") if isinstance(prm.get("resource"), str) else None
    if resource and (not _ok_endpoint(resource, loop) or _origin(resource) != _origin(url)):
        raise McpError("服务器声明的资源地址和它自己的地址不是同一个站点，为安全起见不继续")
    resource = resource or urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, "", ""))
    servers_ = [a for a in prm.get("authorization_servers") or [] if isinstance(a, str)]
    issuer = servers_[0] if servers_ else origin
    if not _ok_endpoint(issuer, loop):
        raise McpError("授权服务器的地址不是 https，不继续")
    i = urllib.parse.urlsplit(issuer)
    iorigin, ipath = f"{i.scheme}://{i.netloc}", i.path.rstrip("/")
    meta = {}
    for c in dict.fromkeys([f"{iorigin}/.well-known/oauth-authorization-server{ipath}",
                            f"{iorigin}/.well-known/openid-configuration{ipath}",
                            f"{iorigin}{ipath}/.well-known/openid-configuration"]):
        meta = await _get_json(c) or {}
        if meta.get("authorization_endpoint") and meta.get("token_endpoint"):
            break
    else:
        if servers_:
            raise McpError("读不到授权服务器的元数据（.well-known/oauth-authorization-server）")
        meta = {"authorization_endpoint": iorigin + "/authorize", "token_endpoint": iorigin + "/token",
                "registration_endpoint": iorigin + "/register"}          # 老规范的默认路径
    out = {"issuer": meta.get("issuer") if isinstance(meta.get("issuer"), str) else issuer, "resource": resource}
    for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
        v = meta.get(k)
        if v is None and k in ("registration_endpoint", "revocation_endpoint"):
            continue
        if not _ok_endpoint(v, loop):
            raise McpError("授权服务器给的地址不是 https，不继续")
        out[k] = v
    methods = meta.get("code_challenge_methods_supported")
    if isinstance(methods, list) and "S256" not in methods:
        raise McpError("授权服务器不支持 PKCE（S256），不继续")
    scopes = prm.get("scopes_supported")
    scope = p.get("scope") or (" ".join(x for x in scopes if isinstance(x, str)) if isinstance(scopes, list) else "")
    out["scope"] = _clip(scope, 1000) if scope else ""
    am = meta.get("token_endpoint_auth_methods_supported")
    out["auth_methods"] = [x for x in am if isinstance(x, str)][:10] if isinstance(am, list) else []
    return out


async def _register(meta: dict) -> dict:
    if not meta.get("registration_endpoint"):
        raise McpError("这个授权服务器不支持自动注册客户端。请先在它那边手动建一个客户端（回调地址填 "
                       + redirect_uri() + "），再把 client id 填到「高级」里。")
    status, _h, raw = await _small("POST", meta["registration_endpoint"], headers={"Accept": "application/json"}, json={
        "client_name": "Roundtable", "redirect_uris": [redirect_uri()], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"})
    try:
        d = json.loads(raw)
    except ValueError:
        d = None
    if status not in (200, 201) or not isinstance(d, dict) or not isinstance(d.get("client_id"), str) or not d["client_id"]:
        why = _clip((d or {}).get("error_description") or (d or {}).get("error") or f"HTTP {status}", 200) if isinstance(d, dict) else f"HTTP {status}"
        raise McpError("在授权服务器上注册客户端失败：" + why)
    secret = d.get("client_secret") if isinstance(d.get("client_secret"), str) else ""
    method = d.get("token_endpoint_auth_method") if secret else "none"
    return {"id": d["client_id"], "secret": secret, "dcr": True,
            "method": method if method in ("client_secret_post", "client_secret_basic", "none") else "client_secret_basic"}


async def _token(sec: dict, form: dict) -> dict:
    meta, client = sec["meta"], sec["client"]
    data = {k: v for k, v in form.items() if v}
    auth = None
    if client.get("secret"):
        if client.get("method") == "client_secret_post":
            data.update(client_id=client["id"], client_secret=client["secret"])
        else:
            auth = httpx.BasicAuth(urllib.parse.quote(client["id"], safe=""), urllib.parse.quote(client["secret"], safe=""))
    else:
        data["client_id"] = client["id"]
    try:
        status, _h, raw = await _small("POST", meta["token_endpoint"], data=data, auth=auth,
                                       headers={"Accept": "application/json"})
    except McpError as e:
        raise OAuthFail(str(e)) from None
    try:
        d = json.loads(raw)
    except ValueError:
        d = {}
    d = d if isinstance(d, dict) else {}
    tok = d.get("access_token")
    if status == 200 and isinstance(tok, str) and re.fullmatch(r"[\x21-\x7e]{1,8000}", tok):
        if str(d.get("token_type") or "bearer").lower() != "bearer":
            raise OAuthFail("授权服务器给的不是 Bearer 令牌", True)
        return d
    code, desc = _clip(d.get("error") or f"HTTP {status}", 80), _clip(d.get("error_description") or "", 200)
    raise OAuthFail(code + ("：" + desc if desc else ""), fatal=400 <= status < 500)


def _with_tokens(sec: dict, d: dict) -> dict:
    exp = d.get("expires_in")
    new_refresh = d.get("refresh_token") if isinstance(d.get("refresh_token"), str) and d.get("refresh_token") else None
    return {**sec, "access": d["access_token"], "refresh": new_refresh or sec.get("refresh"),
            "expires_at": time.time() + float(exp) if isinstance(exp, (int, float)) and exp > 0 else None}


async def _refresh(s: dict) -> None:
    """用刷新令牌换新的访问令牌。授权服务器可能每次都换掉刷新令牌，所以同一个服务器排队做，换到就立刻存。"""
    async with _RLOCK[s["id"]]:
        cur = get(s["id"])
        if not cur or cur["auth"] != "oauth":
            raise McpAuth()
        if cur["secret"].get("access") != s["secret"].get("access"):     # 排队的时候别人已经换好了
            s["secret"] = cur["secret"]
            return
        sec = cur["secret"]
        if not sec.get("refresh"):
            raise McpAuth()
        try:
            d = await _token(sec, {"grant_type": "refresh_token", "refresh_token": sec["refresh"],
                                   "resource": sec["meta"].get("resource")})
        except OAuthFail as e:
            if not e.fatal:
                raise McpError(f"刷新令牌时出错：{e}") from None
            sec = {**sec, "access": None, "refresh": None, "expires_at": None}
            _set(s["id"], secret=sec, status="needs_auth", error="授权失效了，需要重新授权")
            s["secret"] = sec
            raise McpAuth() from None
        sec = _with_tokens(sec, d)
        _set(s["id"], secret=sec)
        s["secret"] = sec


async def _fresh(s: dict) -> None:
    sec = s["secret"]
    if s["auth"] == "oauth" and sec.get("refresh") and sec.get("expires_at") and sec["expires_at"] - time.time() < 60:
        try:
            await _refresh(s)
        except McpAuth:
            raise
        except McpError:
            pass                                  # 临时故障：先拿旧令牌试，真过期了会走 401 那条路


def oauth_start(sid: str) -> str:
    s = get(sid)
    if not s or s["auth"] != "oauth":
        raise ValueError("这个服务器不走 OAuth")
    meta, client = s["secret"]["meta"], s["secret"]["client"]
    now = time.time()
    for k in [k for k, v in PENDING.items() if now - v["at"] > 600 or v["sid"] == sid]:
        PENDING.pop(k, None)
    verifier, st = secrets.token_urlsafe(48), secrets.token_urlsafe(24)
    PENDING[st] = {"sid": sid, "verifier": verifier, "at": now}
    q = {"response_type": "code", "client_id": client["id"], "redirect_uri": redirect_uri(), "state": st,
         "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
         "code_challenge_method": "S256", "resource": meta["resource"]}
    if meta.get("scope"):
        q["scope"] = meta["scope"]
    ep = meta["authorization_endpoint"]
    return ep + ("&" if "?" in ep else "?") + urllib.parse.urlencode(q)


async def oauth_callback(q: dict) -> None:
    """授权服务器把浏览器送回来。结果写进 FLASH，页面打开设置时显示。"""
    p = PENDING.pop(q.get("state") or "", None)
    if not p or time.time() - p["at"] > 600:
        return flash("授权没完成：这次授权已经过期或者不是从这里发起的，请再点一次「授权」。")
    s = get(p["sid"])
    if not s or s["auth"] != "oauth":
        return flash("授权没完成：这个服务器已经被删掉了。")
    if q.get("error"):
        return flash(f"「{s['name']}」授权没完成：" + _clip(q.get("error_description") or q["error"], 200))
    meta = s["secret"]["meta"]
    if q.get("iss") and q["iss"].rstrip("/") != str(meta.get("issuer") or "").rstrip("/"):
        return flash(f"「{s['name']}」授权没完成：回来的授权服务器和发起时的不是同一个。")
    if not q.get("code"):
        return flash(f"「{s['name']}」授权没完成：授权服务器没给授权码。")
    try:
        d = await _token(s["secret"], {"grant_type": "authorization_code", "code": q["code"],
                                       "redirect_uri": redirect_uri(), "code_verifier": p["verifier"],
                                       "resource": meta["resource"]})
    except OAuthFail as e:
        return flash(f"「{s['name']}」授权没完成：换令牌失败（{e}）。")
    s["secret"] = _with_tokens({**s["secret"], "refresh": None}, d)
    _set(s["id"], secret=s["secret"])
    try:
        s = await _load_tools(s)
    except ValueError as e:
        return flash(f"「{s['name']}」：{e}")
    n = len(s["tools"])
    flash(f"「{s['name']}」已授权，有 {n} 个工具。" + ("勾上的才会带给模型。" if n else ""), ok=True, sid=s["id"])


async def _revoke(sec: dict) -> None:
    ep = (sec.get("meta") or {}).get("revocation_endpoint")
    client = sec.get("client") or {}
    for kind in ("refresh", "access"):
        if ep and sec.get(kind) and client.get("id"):
            data = {"token": sec[kind], "token_type_hint": kind + "_token", "client_id": client["id"]}
            if client.get("secret"):
                data["client_secret"] = client["secret"]
            with contextlib.suppress(Exception):
                await _small("POST", ep, data=data)


# ---------- 增删改 ----------

def _slug(name: str, url: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if len(base) < 2 or not base[0].isalpha():
        label = re.sub(r"^mcp[-_]?", "", (urllib.parse.urlsplit(url).hostname or "").split(".")[0].lower())
        base = re.sub(r"[^a-z0-9]+", "_", label).strip("_")
        if len(base) < 2 or not base[0].isalpha():
            base = "srv"
    base = base[:10].strip("_")
    taken = {r["slug"] for r in db.q("SELECT slug FROM mcp_servers")}
    slug, i = base, 2
    while slug in taken:
        slug = f"{base[:9].strip('_')}_{i}"
        i += 1
    return slug


async def _load_tools(s: dict) -> dict:
    """连上游、取工具表、存起来。出错时把状态记到库里再抛 ValueError。"""
    _SESS.pop(s["id"], None)
    try:
        st = await _init(s)
        tools = await list_tools(s)
    except McpAuth as e:
        _mark_auth(s)
        raise ValueError("需要重新授权" if s["auth"] == "oauth" else str(e)) from None
    except McpError as e:
        _set(s["id"], status="error", error=str(e))
        raise ValueError(str(e)) from None
    _set(s["id"], info=st["info"], tools=_merge_tools(s["tools"], tools), status="ok", error=None)
    return get(s["id"])


async def add_server(name: str, url: str, token: str = "", header: str = "", client_id: str = "",
                     client_secret: str = "") -> dict:
    """加一个服务器。连不上就不保存。需要 OAuth 的先存成「需要授权」，由用户点授权。"""
    name, token, header = (name or "").strip()[:40], (token or "").strip(), (header or "").strip()
    client_id, client_secret = (client_id or "").strip(), (client_secret or "").strip()
    url = check_url(url)
    if len(db.q("SELECT 1 FROM mcp_servers")) >= config.MCP_MAX_SERVERS:
        raise ValueError(f"最多接 {config.MCP_MAX_SERVERS} 个")
    if db.q1("SELECT 1 FROM mcp_servers WHERE url=?", (url,)):
        raise ValueError("这个地址已经加过了")
    s = {"id": db.new_id(), "url": url, "auth": "none", "secret": {}, "tools": [], "info": {}, "status": "ok"}
    if token:
        if header and (not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header) or header.lower() in BAD_HEADERS):
            raise ValueError("请求头的名字不对")
        if not re.fullmatch(r"[\x20-\x7e]{1,8000}", token):
            raise ValueError("令牌里有不能放进请求头的字符")
        if not header or header.lower() == "authorization":
            header = "Authorization"
            token = token if re.match(r"(?i)(bearer|basic) ", token) else "Bearer " + token
        s.update(auth="header", secret={"header": header, "value": token})
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        st = await _init(s)
        tools = await list_tools(s)
    except McpAuth as e:
        _SESS.pop(s["id"], None)
        if token:
            raise ValueError("服务器不认这个令牌（401）") from None
        try:
            meta = await _discover(url, e.www)
            if client_id:
                am = meta.get("auth_methods") or []
                client = {"id": client_id, "secret": client_secret, "dcr": False,
                          "method": "none" if not client_secret else
                          "client_secret_post" if am and "client_secret_basic" not in am else "client_secret_basic"}
            else:
                client = await _register(meta)
        except McpError as e2:
            raise ValueError(f"服务器要求登录。它要是用固定令牌，把令牌填在「高级」里再加；要是用 OAuth，这一步没走通：{e2}") from None
        s.update(auth="oauth", secret={"meta": meta, "client": client}, status="needs_auth", name=name or host,
                 error="还没授权")
        s["slug"] = _slug(s["name"], url)
        _insert(s)
        return get(s["id"])
    except McpError as e:
        _SESS.pop(s["id"], None)
        raise ValueError(str(e)) from None
    s.update(name=name or _clip(st["info"].get("name") or "", 40).strip() or host, info=st["info"],
             tools=_merge_tools([], tools))
    s["slug"] = _slug(s["name"], url)
    _insert(s)
    return get(s["id"])


def patch_server(sid: str, d: dict) -> None:
    s = get(sid)
    if not s:
        raise ValueError("没有这个服务器")
    kw = {}
    if "enabled" in d:
        kw["enabled"] = 1 if d["enabled"] else 0
    if isinstance(d.get("name"), str) and d["name"].strip():
        kw["name"] = d["name"].strip()[:40]
    if isinstance(d.get("tools"), dict):
        for t in s["tools"]:
            if t["name"] in d["tools"]:
                t["enabled"] = bool(d["tools"][t["name"]])
                if t["enabled"]:
                    t["changed"] = False
        kw["tools"] = s["tools"]
    _set(sid, **kw)


async def refresh_server(sid: str) -> None:
    s = get(sid)
    if not s:
        raise ValueError("没有这个服务器")
    await _load_tools(s)


async def delete_server(sid: str) -> None:
    s = get(sid)
    if not s:
        return
    db.x("DELETE FROM mcp_servers WHERE id=?", (sid,))
    _SESS.pop(sid, None)
    for k in [k for k, v in PENDING.items() if v["sid"] == sid]:
        PENDING.pop(k, None)
    if s["auth"] == "oauth":
        t = asyncio.create_task(_revoke(s["secret"]))     # 尽力而为，不等它
        _BG.add(t)
        t.add_done_callback(_BG.discard)


_BG: set = set()


# ---------- 一次回答里的 MCP ----------

RUNS: dict[str, "RunCtx"] = {}                    # 运行令牌 -> 这次回答


def _flatten(res: dict) -> tuple[str, bool]:
    """工具结果压成一段文字（三种后端都只收文字）。"""
    parts = []
    for c in res.get("content") or []:
        if not isinstance(c, dict):
            continue
        kind = c.get("type")
        if kind == "text":
            parts.append(str(c.get("text") or ""))
        elif kind == "resource" and isinstance(c.get("resource"), dict) and isinstance(c["resource"].get("text"), str):
            parts.append(c["resource"]["text"])
        elif kind == "resource_link":
            parts.append(f"[资源链接] {c.get('uri') or ''}")
        else:
            parts.append(f"[这里有一段 {kind or '未知'} 类型的内容，圆桌只转文字，已略去]")
    text = "\n".join(p for p in parts if p)
    if not text and res.get("structuredContent") is not None:
        text = json.dumps(res["structuredContent"], ensure_ascii=False)
    text = text or "（工具没有返回内容）"
    if len(text) > config.MCP_RESULT_CAP:
        text = text[:config.MCP_RESULT_CAP] + f"\n…（结果太长，后面 {len(text) - config.MCP_RESULT_CAP} 个字符被截掉了）"
    return text, bool(res.get("isError"))


def _result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class RunCtx:
    """一次回答能用的 MCP 工具（开跑时拍的快照），以及它的运行令牌。"""

    def __init__(self, sink, srv: dict):
        self.token = secrets.token_urlsafe(32)
        self.sink = sink
        self.servers = srv                        # slug -> {"id", "name", "tools": {x: 工具}}
        self.fn = {f"{slug}__{x}": (slug, x) for slug, s in srv.items() for x in s["tools"]}
        self.closed = False                       # 不再接新的调用（回答在收尾）
        self.done = False                         # 回答已经结束，痕迹也不再写
        self.calls = 0
        self.pending: dict[str, dict] = {}
        self.tasks: set = set()

    def names(self) -> list[str]:
        return [s["name"] for s in self.servers.values()]

    # --- 给 claude / codex：连本机网关 ---

    def gateway_error(self) -> str | None:
        return None if GW["port"] else "MCP 网关没起来（" + (GW["error"] or "原因不明") + "），走命令行的模型（Claude、ChatGPT、CodeBuddy）这次用不了 MCP。可以先在设置里把 MCP 关掉。"

    def _url(self, slug: str) -> str:
        return f"http://127.0.0.1:{GW['port']}/mcp/{slug}"

    def claude_config(self) -> dict:
        return {"mcpServers": {slug: {"type": "http", "url": self._url(slug),
                                      "headers": {"Authorization": "Bearer " + self.token}} for slug in self.servers}}

    def claude_allowed(self) -> list[str]:
        return [f"mcp__{slug}__{x}" for slug, s in self.servers.items() for x in s["tools"]]

    def codex_args(self) -> list[str]:
        out = []
        for slug, s in self.servers.items():
            p = f"mcp_servers.{slug}."
            for kv in ("enabled=true", f'url="{self._url(slug)}"', 'bearer_token_env_var="RT_MCP_TOKEN"',
                       "enabled_tools=" + json.dumps(list(s["tools"])), "startup_timeout_sec=20",
                       f"tool_timeout_sec={config.MCP_CALL_TIMEOUT + 20}",
                       *(f'tools.{x}.approval_mode="approve"' for x in s["tools"])):
                out += ["-c", p + kv]
        return out

    # --- 给 OpenRouter：函数调用 ---

    def openai_tools(self) -> list[dict]:
        return [{"type": "function", "function": {
            "name": f"{slug}__{x}", "description": _clip(f"[{s['name']}] {t.get('description') or t['name']}", 1024),
            "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}}
            for slug, s in self.servers.items() for x, t in s["tools"].items()]

    # --- 调用 ---

    def _emit(self, item: dict) -> None:
        if not self.done:
            self.sink.activity(item)

    async def call(self, slug: str, x: str, args) -> dict:
        """调一个工具。不抛业务异常：失败也作为“工具报错”的结果还给模型。"""
        srv = self.servers.get(slug)
        tool = srv["tools"].get(x) if srv else None
        if not tool:
            return _result("没有这个工具，或者用户没有给你开这个工具。", True)
        if self.closed:
            return _result("这次回答已经结束。", True)
        self.calls += 1
        if self.calls > config.MCP_MAX_CALLS:
            return _result(f"这次回答里的工具调用已经超过 {config.MCP_MAX_CALLS} 次上限，不能再调了。请根据已有的信息作答。", True)
        args = args if isinstance(args, dict) else {}
        cid = secrets.token_hex(5)
        item = {"kind": "mcp", "id": cid, "text": f"{srv['name']} · {tool['name']}",
                "args": _clip(json.dumps(args, ensure_ascii=False), 2000), "pending": True}
        self.pending[cid] = item
        self._emit(item)
        t0 = time.time()
        try:
            s = get(srv["id"])
            if not s or not _live(s) or not any(t["name"] == tool["name"] and t.get("enabled") for t in s["tools"]):
                raise McpError("用户刚刚在设置里把这个工具关掉了")
            text, bad = _flatten(await rpc(s, "tools/call", {"name": tool["name"], "arguments": args},
                                           timeout=config.MCP_CALL_TIMEOUT))
        except McpError as e:
            text, bad = f"工具调用失败：{e}", True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("mcp call:", type(e).__name__, flush=True)
            text, bad = f"工具调用失败：{type(e).__name__}", True
        if self.pending.pop(cid, None) is not None:
            self._emit({**item, "pending": False, "ms": int((time.time() - t0) * 1000),
                        ("error" if bad else "result"): _clip(text, 4000)})
        return _result(text, bad)


def open_run(sink) -> RunCtx | None:
    """给一次回答准备 MCP。没有可用的工具就返回 None（模型那边完全看不到 MCP）。"""
    srv = {}
    for s in servers():
        if not _live(s):
            continue
        tools = {t["x"]: t for t in s["tools"] if t.get("enabled") and t.get("x")}
        if tools:
            srv[s["slug"]] = {"id": s["id"], "name": s["name"], "tools": tools}
    if not srv:
        return None
    ctx = RunCtx(sink, srv)
    RUNS[ctx.token] = ctx
    return ctx


def close_run(ctx: RunCtx | None) -> None:
    if not ctx:
        return
    RUNS.pop(ctx.token, None)
    for cid, item in list(ctx.pending.items()):
        ctx.pending.pop(cid, None)
        ctx._emit({**item, "pending": False, "error": "已中止"})
    ctx.closed = ctx.done = True
    for t in list(ctx.tasks):
        t.cancel()


# ---------- 本机网关（只给 claude / codex 子进程用） ----------

GW: dict = {"port": None, "error": None, "server": None, "task": None}


def _rpc_err(mid, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


async def _gw_handle(ctx: RunCtx, slug: str, msg) -> dict | None:
    if not isinstance(msg, dict):
        return _rpc_err(None, -32600, "invalid request")
    method, mid = msg.get("method"), msg.get("id")
    if not isinstance(method, str) or "id" not in msg:
        return None                               # 通知，或者对方给我们的回应：不用答
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    srv = ctx.servers[slug]
    if method == "initialize":
        v = params.get("protocolVersion")
        res = {"protocolVersion": v if v in PROTOS else PROTO, "capabilities": {"tools": {}},
               "serverInfo": {"name": slug, "title": srv["name"], "version": "1"}}
    elif method == "ping":
        res = {}
    elif method == "tools/list":
        res = {"tools": [{"name": x, "description": t.get("description") or "",
                          "inputSchema": t.get("inputSchema") or {"type": "object", "properties": {}},
                          **({"annotations": {"readOnlyHint": True}} if t.get("ro") else {})}
                         for x, t in srv["tools"].items()]}
    elif method == "tools/call":
        task = asyncio.current_task()
        ctx.tasks.add(task)
        try:
            res = await ctx.call(slug, str(params.get("name") or ""), params.get("arguments"))
        except asyncio.CancelledError:
            res = _result("已中止。", True)       # 这次回答被停掉了；把话说完，别让 uvicorn 打一屏堆栈
        finally:
            ctx.tasks.discard(task)
    elif method == "resources/list":
        res = {"resources": []}
    elif method == "resources/templates/list":
        res = {"resourceTemplates": []}
    elif method == "prompts/list":
        res = {"prompts": []}
    else:
        return _rpc_err(mid, -32601, "method not found")
    return {"jsonrpc": "2.0", "id": mid, "result": res}


async def _gw_reply(send, status: int, obj=None) -> None:
    body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"), (b"cache-control", b"no-store"),
        (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


async def gateway_app(scope, receive, send):
    """独立的小 ASGI 应用，只听 127.0.0.1。不过主应用的 Guard，所以自己把关：
    只认本机直连（见到任何代理头就拒），只认当前正在跑的回答的运行令牌。"""
    if scope["type"] != "http":
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 1008})
        return
    h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
    client = (scope.get("client") or ("",))[0]
    port = GW["port"]
    if (client not in ("127.0.0.1", "::1") or h.get("host") not in (f"127.0.0.1:{port}", f"localhost:{port}")
            or "origin" in h or "forwarded" in h or "via" in h or any(k.startswith("x-forwarded-") for k in h)
            or "x-real-ip" in h):
        return await _gw_reply(send, 403, {"error": "forbidden"})
    m = re.fullmatch(r"/mcp/([a-z0-9_]{1,16})", scope["path"])
    if not m:
        return await _gw_reply(send, 404, {"error": "not found"})
    auth = h.get("authorization") or ""
    ctx = RUNS.get(auth[7:]) if auth[:7].lower() == "bearer " else None
    if not ctx or ctx.closed or m.group(1) not in ctx.servers:
        return await _gw_reply(send, 401, {"error": "unauthorized"})
    if scope["method"] != "POST":
        return await _gw_reply(send, 405, {"error": "method not allowed"})
    body = bytearray()
    while True:
        ev = await receive()
        if ev["type"] != "http.request":
            return
        body += ev.get("body") or b""
        if len(body) > BODY_CAP:
            return await _gw_reply(send, 413, {"error": "too large"})
        if not ev.get("more_body"):
            break
    try:
        data = json.loads(bytes(body))
    except ValueError:
        return await _gw_reply(send, 400, _rpc_err(None, -32700, "parse error"))
    if isinstance(data, list):
        out = [r for r in [await _gw_handle(ctx, m.group(1), one) for one in data[:50]] if r is not None]
        return await _gw_reply(send, 200, out) if out else await _gw_reply(send, 202)
    out = await _gw_handle(ctx, m.group(1), data)
    return await _gw_reply(send, 200, out) if out is not None else await _gw_reply(send, 202)


class _Quiet(uvicorn.Server):
    """跑在主应用事件循环里的第二个 uvicorn：不能碰信号（主 uvicorn 管着）。"""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self) -> None:
        pass


async def start_gateway() -> None:
    """端口自己先占好再交给 uvicorn：占不到只是 MCP 对 claude / codex 不可用，主应用照常起。
    ROUNDTABLE_MCP_PORT 不填（0）就让系统挑一个空闲端口。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", config.MCP_GATEWAY_PORT))
    except OSError as e:
        sock.close()
        GW["error"] = f"端口 {config.MCP_GATEWAY_PORT or '（自动）'} 占不到：{e.strerror or e}"
        print("mcp gateway:", GW["error"], flush=True)
        return
    sock.set_inheritable(False)
    srv = _Quiet(uvicorn.Config(gateway_app, log_level="warning", access_log=False, lifespan="off", ws="none",
                                server_header=False, timeout_keep_alive=30, timeout_graceful_shutdown=1))
    GW.update(port=sock.getsockname()[1], error=None, server=srv, task=asyncio.create_task(srv.serve(sockets=[sock])))

    def done(t: asyncio.Task) -> None:
        GW["port"] = None
        if not t.cancelled() and t.exception():
            GW["error"] = f"网关退出了：{type(t.exception()).__name__}"
            print("mcp gateway:", GW["error"], flush=True)
    GW["task"].add_done_callback(done)


async def stop_gateway() -> None:
    for ctx in list(RUNS.values()):
        close_run(ctx)
    if GW["server"]:
        GW["server"].should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(GW["task"]), 3)
    global _http
    if _http is not None:
        with contextlib.suppress(Exception):
            await _http.aclose()
        _http = None
