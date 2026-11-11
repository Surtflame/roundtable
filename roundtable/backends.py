"""四个后端：claude -p（Claude Code）、codex exec（Codex CLI）、codebuddy -p（腾讯 CodeBuddy Code）、
OpenRouter（HTTPS，按量）。哪个装了、配了就用哪个，一个都没有也能启动（只是没有模型可选）。

统一接口：run(model_id, effort, system, parts, web, sink, ...)。
parts 是引擎拼好的“中性”提示词片段：
  {"t":"text","s":...} / {"t":"image","f":file} / {"t":"pdf","f":file} / {"t":"doc","f":file}
出错抛 BackendError；被取消时负责把子进程杀干净。

隔离原则：聊天里的模型只能“说话 + 联网搜索 + 画图 + 用户在设置里接的 MCP 工具”，碰不到服务器。
  - claude：--restricted，工具只留 WebSearch/WebFetch，不加载设置文件、skills，也不加载机器上配的 MCP。
  - codex：关掉 shell/插件/应用等工具，不读用户的 config.toml（或者把里面的 MCP 服务器逐个关掉），
    外面再套一层 bwrap（有的话），只看得到系统目录和 ~/.codex。
  - codebuddy：工具只留联网搜索/抓网页（有图片时加一个看图工具），不读设置文件、钩子、说明文件、skills、插件，
    也不加载机器上配的 MCP。它没有 --restricted 这样的总开关，靠的是下面 CODEBUDDY_ENV 和 _codebuddy_common 里那几项。
  - OpenRouter：纯 HTTP。
  - MCP（见 mcp.py）：参数 mcp 是这次回答的 RunCtx，没接就是 None。claude / codex 只拿到本机网关的地址和
    一次性的运行令牌，上游服务器的令牌不给它们；OpenRouter 的工具调用由这里的循环转给 RunCtx.call()。
启动时 probe() 会看本机装的命令行工具支持哪些参数；隔离做不到的后端直接标成不可用，而不是降级硬跑。
"""
import asyncio
import base64
import json
import os
import re
import secrets
import shutil
import signal
import time
import tomllib
from pathlib import Path

import httpx

from . import config, files


class BackendError(Exception):
    pass


class Sink:
    """引擎实现这些回调。"""

    def text(self, s: str) -> None: ...
    def thinking(self, s: str) -> None: ...
    def activity(self, item: dict) -> None: ...
    def image(self, data: bytes, name: str = "") -> None: ...
    def usage(self, u: dict) -> None: ...


STATE: dict = {"claude_rate_limit": None}


def claude_model_seen(asked: str, actual: str) -> None:
    """Claude Code 报出了 --model 给的名字实际解析成哪个模型 id。引擎启动时换成它自己的实现（记下来，界面上显示版本号）。"""
# probe() 填。ok=False 时 why 是给人看的原因。
CAPS: dict = {
    "claude": {"ok": False, "why": "还没检测", "version": "", "flags": set(), "isolate": []},
    "codex": {"ok": False, "why": "还没检测", "version": "", "disable": [], "flags": [], "sandbox": "off", "note": "",
              "wrap": [], "exe": [], "builtin_models": []},
    # models / account 是问 CLI 问来的（见 codebuddy_refresh）
    "codebuddy": {"ok": False, "why": "还没检测", "version": "", "flags": set(), "models": [], "account": None,
                  "asked": False, "at": 0.0},
}


# ---------- 提示词片段 → 各家格式 ----------

def _file_text_block(f: dict) -> str:
    m = f.get("meta") or {}
    attrs = f'name="{f["name"]}"'
    if m.get("pages"):
        attrs += f' pages="{m["pages"]}"'
    body = files.read_text(f)
    note = "\n[文件过长，以上内容已截断]" if m.get("truncated") else ""
    if not body.strip():
        body = "[这个文件里没有抽取到文字]"
    return f"<file {attrs}>\n{body}{note}\n</file>"


def _merge_text(blocks: list[dict], s: str, key: str = "text") -> None:
    if blocks and blocks[-1].get("type") == "text":
        blocks[-1][key] += "\n" + s
    else:
        blocks.append({"type": "text", key: s})


def parts_to_claude(parts: list[dict]) -> list[dict]:
    budget = 20 * 1024 * 1024
    out: list[dict] = []
    for p in parts:
        if p["t"] == "text":
            _merge_text(out, p["s"])
        elif p["t"] == "image":
            f = p["f"]
            _merge_text(out, f"[图片：{f['name']}]")
            out.append({"type": "image", "source": {"type": "base64", "media_type": f["mime"],
                                                    "data": files.read_b64(f)}})
        elif p["t"] == "pdf":
            f = p["f"]
            m = f.get("meta") or {}
            if f["size"] <= budget and (m.get("pages") or 0) <= 100:
                budget -= f["size"]
                out.append({"type": "document", "title": f["name"],
                            "source": {"type": "base64", "media_type": "application/pdf",
                                       "data": files.read_b64(f)}})
            elif m.get("scanned"):
                _merge_text(out, f"[扫描版 PDF：{f['name']}，以下是前几页的图像]")
                for pg in files.pdf_page_images(f):
                    out.append({"type": "image", "source": {
                        "type": "base64", "media_type": "image/jpeg",
                        "data": base64.b64encode(pg.read_bytes()).decode()}})
            else:
                _merge_text(out, _file_text_block(f))
        elif p["t"] == "doc":
            _merge_text(out, _file_text_block(p["f"]))
    return out


def parts_to_flat(parts: list[dict], vision: bool) -> tuple[str, list[tuple[Path, str]]]:
    """→ (整段文字, [(图片路径, mime)])。图片在文字里用 [图片 N] 占位。"""
    texts: list[str] = []
    images: list[tuple[Path, str]] = []

    def add_image(path: Path, mime: str, name: str) -> None:
        if not vision:
            texts.append(f"[图片：{name}（该模型不支持图像输入，已省略）]")
            return
        images.append((path, mime))
        texts.append(f"[图片 {len(images)}：{name}]")

    for p in parts:
        if p["t"] == "text":
            texts.append(p["s"])
        elif p["t"] == "image":
            add_image(Path(p["f"]["path"]), p["f"]["mime"], p["f"]["name"])
        elif p["t"] == "pdf":
            f = p["f"]
            if (f.get("meta") or {}).get("scanned") and vision:
                texts.append(f"[扫描版 PDF：{f['name']}，以下是前几页的图像]")
                for i, pg in enumerate(files.pdf_page_images(f), 1):
                    add_image(pg, "image/jpeg", f"{f['name']} 第 {i} 页")
            else:
                texts.append(_file_text_block(f))
        elif p["t"] == "doc":
            texts.append(_file_text_block(p["f"]))
    return "\n".join(texts), images


def parts_to_openrouter(parts: list[dict], vision: bool) -> list[dict]:
    out: list[dict] = []

    def add_image(path: Path, mime: str, name: str) -> None:
        if not vision:
            _merge_text(out, f"[图片：{name}（该模型不支持图像输入，已省略）]")
            return
        _merge_text(out, f"[图片：{name}]")
        b64 = base64.b64encode(path.read_bytes()).decode()
        out.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

    for p in parts:
        if p["t"] == "text":
            _merge_text(out, p["s"])
        elif p["t"] == "image":
            add_image(Path(p["f"]["path"]), p["f"]["mime"], p["f"]["name"])
        elif p["t"] == "pdf":
            f = p["f"]
            if (f.get("meta") or {}).get("scanned") and vision:
                _merge_text(out, f"[扫描版 PDF：{f['name']}，以下是前几页的图像]")
                for i, pg in enumerate(files.pdf_page_images(f), 1):
                    add_image(pg, "image/jpeg", f"{f['name']} 第 {i} 页")
            else:
                _merge_text(out, _file_text_block(f))
        elif p["t"] == "doc":
            _merge_text(out, _file_text_block(p["f"]))
    return out


# ---------- 子进程工具 ----------

async def _run_quick(args: list[str], timeout: float = 25) -> tuple[int, str]:
    """跑一个很快就结束的命令，→ (退出码, 标准输出+错误输出)。起不来返回 (-1, 原因)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=config.cli_env(), cwd=str(config.EMPTY_DIR),
            start_new_session=True)
    except OSError as e:
        return -1, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode or 0, out.decode("utf-8", "replace")
    except asyncio.TimeoutError:
        return -1, "超时"
    finally:
        await _kill(proc)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            await asyncio.wait_for(proc.wait(), 3)
            return
        except asyncio.TimeoutError:
            continue


async def _drain_stderr(proc: asyncio.subprocess.Process, buf: list[str]) -> None:
    try:
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            buf.append(chunk.decode("utf-8", "replace"))
            if len(buf) > 40:
                del buf[:20]
    except Exception:
        return


async def _feed_stdin(proc: asyncio.subprocess.Process, data: bytes) -> None:
    try:
        proc.stdin.write(data)
        await proc.stdin.drain()
        proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        pass


# ---------- Claude ----------

def _mcp_ready(mcp) -> None:
    err = mcp.gateway_error() if mcp else None
    if err:
        raise BackendError(err)


def _mcp_env(mcp) -> dict:
    """子进程的环境变量。接了 MCP 时保证本机网关不走代理。"""
    env = config.cli_env()
    if mcp:
        for k in ("NO_PROXY", "no_proxy"):
            env[k] = ",".join(x for x in (env.get(k, ""), "127.0.0.1,localhost") if x)
    return env


async def run_claude(model: str, effort: str, system: str, parts: list[dict], web: bool, sink: Sink,
                     mcp=None) -> None:
    cap = CAPS["claude"]
    if not cap["ok"]:
        raise BackendError("Claude 用不了：" + cap["why"])
    _mcp_ready(mcp)
    if mcp and "--mcp-config" not in cap["flags"]:
        raise BackendError("这个版本的 Claude Code 没有 --mcp-config 参数，接不了 MCP。请升级，或者先在设置里把 MCP 停用。")
    args = [config.CLAUDE_BIN, "-p", "--model", model, "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose", "--system-prompt", system] + _claude_common()
    if "--include-partial-messages" in cap["flags"]:
        args.append("--include-partial-messages")           # 逐字流式；没有就整段出来
    builtin = ["WebSearch", "WebFetch"] if web else []
    args += ["--tools", ",".join(builtin)]
    env = _mcp_env(mcp)
    mdir = None
    if mcp:
        # 只认这一个配置文件（--strict-mcp-config），里面只有本机网关；--allowedTools 逐个列出勾选过的工具
        mdir = config.RUNS_DIR / ("mcp-" + secrets.token_hex(8))
        mdir.mkdir(parents=True, mode=0o700)
        fd = os.open(mdir / "mcp.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(mcp.claude_config(), f)
        args += ["--mcp-config", str(mdir / "mcp.json")]
        env["MAX_MCP_OUTPUT_TOKENS"] = "60000"
    if builtin or mcp:
        args += ["--allowedTools", ",".join(builtin + (mcp.claude_allowed() if mcp else []))]
    if effort and "--effort" in cap["flags"]:
        args += ["--effort", effort]
    try:
        content = await asyncio.to_thread(parts_to_claude, parts)
        line = json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False)
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(config.EMPTY_DIR), env=env, start_new_session=True, limit=256 * 1024 * 1024)
    except BaseException:
        if mdir:
            shutil.rmtree(mdir, ignore_errors=True)
        raise
    errbuf: list[str] = []
    err_task = asyncio.create_task(_drain_stderr(proc, errbuf))
    feed_task = asyncio.create_task(_feed_stdin(proc, line.encode() + b"\n"))
    got_text = False
    got_thinking = False
    need_sep = False
    seen_tools: set[str] = set()
    result = None
    actual = ""                                   # 别名（opus 之类）实际解析成的模型 id

    def saw_model(mid) -> None:
        nonlocal actual
        if not actual and isinstance(mid, str) and mid.startswith("claude-"):
            actual = mid
            sink.usage({"model": mid})            # 现在就记：中途被停掉的回答也知道是谁答的
            claude_model_seen(model, mid)

    try:
        async for raw in proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "stream_event":
                e = ev.get("event") or {}
                et = e.get("type")
                if et == "content_block_start":
                    bt = (e.get("content_block") or {}).get("type")
                    if bt == "text" and got_text:
                        need_sep = True
                    elif bt == "thinking" and got_thinking:
                        sink.thinking("\n\n")
                elif et == "content_block_delta":
                    d = e.get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        if need_sep:
                            sink.text("\n\n")
                            need_sep = False
                        sink.text(d["text"])
                        got_text = True
                    elif d.get("type") == "thinking_delta" and d.get("thinking"):
                        sink.thinking(d["thinking"])
                        got_thinking = True
            elif t == "system" and ev.get("subtype") == "init":
                saw_model(ev.get("model"))
            elif t == "assistant":
                if not ev.get("parent_tool_use_id"):
                    saw_model((ev.get("message") or {}).get("model"))     # 旧版本的 init 里没有 model 时靠这个
                for b in (ev.get("message") or {}).get("content") or []:
                    if b.get("type") == "tool_use" and b.get("id") not in seen_tools:
                        seen_tools.add(b.get("id"))
                        inp = b.get("input") or {}
                        if b.get("name") == "WebSearch":
                            sink.activity({"kind": "search", "text": inp.get("query", "")})
                        elif b.get("name") == "WebFetch":
                            sink.activity({"kind": "fetch", "text": inp.get("url", "")})
                        elif str(b.get("name") or "").startswith("mcp__"):
                            pass                  # MCP 调用由 mcp.RunCtx.call 记（带参数和结果）
                        else:
                            sink.activity({"kind": "tool", "text": b.get("name", "")})
            elif t == "rate_limit_event":
                info = ev.get("rate_limit_info") or {}
                STATE["claude_rate_limit"] = {**info, "seen_at": time.time()}
            elif t == "result":
                result = ev
        await proc.wait()
    finally:
        if mcp:
            mcp.closed = True                     # 先关门再杀进程：CLI 收到 SIGTERM 时会把没做完的工具调用重发一遍
        await _kill(proc)
        for tk in (err_task, feed_task):
            tk.cancel()
        if mdir:
            shutil.rmtree(mdir, ignore_errors=True)

    if result is None:
        tail = "".join(errbuf)[-800:].strip()
        raise BackendError(f"claude 没有返回结果（退出码 {proc.returncode}）{('：' + tail) if tail else ''}")
    u = result.get("usage") or {}
    sink.usage({
        "in": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
        + (u.get("cache_creation_input_tokens") or 0),
        "out": u.get("output_tokens") or 0,
        "cost_est": result.get("total_cost_usd"),
        "searches": sum((m.get("webSearchRequests") or 0) for m in (result.get("modelUsage") or {}).values()),
    })
    if result.get("is_error"):
        msg = result.get("result") or result.get("subtype") or "未知错误"
        raise BackendError(str(msg)[:1500])
    if not got_text and result.get("result"):
        sink.text(result["result"])


async def quick_claude(prompt: str, model: str = "haiku", timeout: float = 60) -> str:
    """一次性小任务（起标题）。不带任何工具。失败返回空串。"""
    if not CAPS["claude"]["ok"]:
        return ""
    args = [config.CLAUDE_BIN, "-p", "--model", model, "--output-format", "json", "--tools", "",
            "--system-prompt", "你是一个只输出结果、不加任何解释的小工具。"] + _claude_common()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=str(config.EMPTY_DIR), env=config.cli_env(), start_new_session=True)
    except OSError:
        return ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
        d = json.loads(out)
        used = [k for k in (d.get("modelUsage") or {}) if isinstance(k, str) and k.startswith("claude-")]
        if len(used) == 1:
            claude_model_seen(model, used[0])
        return "" if d.get("is_error") else (d.get("result") or "").strip()
    except Exception:
        return ""
    finally:
        await _kill(proc)


def _claude_common() -> list[str]:
    cap = CAPS["claude"]
    return cap["isolate"] + [f for f in ("--strict-mcp-config", "--no-session-persistence",
                                         "--disable-slash-commands") if f in cap["flags"]]


async def probe_claude() -> None:
    cap = CAPS["claude"]
    cap.update(ok=False, version="", flags=set(), isolate=[])
    if not config.CLAUDE_BIN:
        cap["why"] = "没有找到 claude 命令（没装 Claude Code，或者 ROUNDTABLE_CLAUDE_BIN=off）"
        return
    code, out = await _run_quick([config.CLAUDE_BIN, "--help"])
    if code != 0:
        cap["why"] = f"claude --help 运行失败：{out.strip()[-200:]}"
        return
    flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+", out))
    cap["flags"] = flags
    _, ver = await _run_quick([config.CLAUDE_BIN, "--version"])
    cap["version"] = ver.strip().splitlines()[0][:60] if ver.strip() else ""
    need = {"--tools", "--system-prompt", "--input-format", "--output-format", "--strict-mcp-config"}
    if need - flags:
        cap["why"] = "Claude Code 版本太旧，缺少参数 " + " ".join(sorted(need - flags)) + "，请升级"
        return
    if "--restricted" in flags:
        cap["isolate"] = ["--restricted"]
    elif "--setting-sources" in flags:
        cap["isolate"] = ["--setting-sources", ""]          # 不读任何设置文件（钩子、权限放行都在里面）
    else:
        cap["why"] = "Claude Code 版本太旧，没有 --restricted 也没有 --setting-sources，没法隔离，请升级"
        return
    cap.update(ok=True, why="")


# ---------- CodeBuddy ----------
# 腾讯的 CodeBuddy Code。命令行参数、stream-json 的事件格式都和 Claude Code 基本一样，所以这一段长得像上面那段。
# 实测（2.183）出来的几处不一样，都是隔离要靠的东西，改之前先看 docs/backends.md 里“CodeBuddy 的隔离”：
#   - --setting-sources 写空串等于没写，要写 none 才是“什么设置都不读”（钩子就在设置里）
#   - 用户级的 CODEBUDDY.md、它自己的“长期记忆”不归 --setting-sources 管，各有一个环境变量
#   - --tools 管的是全部内置工具；MCP 工具默认“延迟加载”（模型要先用 ToolSearch 才看得到），关掉延迟才直接可用
#   - 抓网页默认先走它的远程接口、失败了从本机直接抓。本机直抓它自己会拦内网 IP，但拦不住“解析到内网的域名”
#     （实测），所以默认关掉本机直抓；ROUNDTABLE_CODEBUDDY_LOCAL_FETCH=on 可以放开
#   - --allowedTools / --permission-mode dontAsk 只管它认为“要问一声”的工具（比如抓网页：不点名就拒绝）；
#     联网搜索和 MCP 工具它不问，不点名也照样执行（实测）。所以真正的闸门是 --tools 给了哪些内置工具、
#     网关给了哪些 MCP 工具
#   - 图片：有的模型直接收图，有的靠一个 ViewImage 工具转述，由 CLI 自己判断，所以有图时把 ViewImage 也放开

CODEBUDDY_ENV = {
    "CODEBUDDY_DISABLE_AUTO_MEMORY": "1",
    "CODEBUDDY_DISABLE_SYSTEM_REMINDER_MD": "1",
    "CODEBUDDY_SKIP_BUILTIN_MARKETPLACE": "1",
    "CODEBUDDY_DEFER_TOOL_LOADING": "0",
    "DISABLE_AUTOUPDATER": "1",
}
_cb_lock = asyncio.Lock()
_cb_tasks: set = set()


def _codebuddy_ttl() -> float:
    """模型清单、登录状态隔多久重新问一次（秒）。还没登录时问得勤一点，登录完很快就能看到。"""
    return 1800 if CAPS["codebuddy"]["account"] else 120


def _codebuddy_env(mcp=None) -> dict:
    env = {**_mcp_env(mcp), **CODEBUDDY_ENV}
    if not config.CODEBUDDY_LOCAL_FETCH:
        env["CODEBUDDY_WEBFETCH_DISABLE_LOCAL_FALLBACK"] = "1"
    return env


def _codebuddy_common() -> list[str]:
    flags = CAPS["codebuddy"]["flags"]
    # dontAsk：要人点头才能用的工具，没列进 --allowedTools 的直接拒绝（这里本来就没有人可以问）
    return ["--setting-sources", "none", "--strict-mcp-config"] + (
        ["--permission-mode", "dontAsk"] if "--permission-mode" in flags else []) + [
        f for f in ("--no-session-persistence",) if f in flags]


def parts_to_codebuddy(parts: list[dict]) -> list[dict]:
    """和给 Claude 的一样，只是 PDF 不整份传（它会原样转给模型，国内的模型大多不收文件），改成抽出来的文字。"""
    out: list[dict] = []

    def add_image(data: str, mime: str, name: str) -> None:
        _merge_text(out, f"[图片：{name}]")
        out.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})

    for p in parts:
        if p["t"] == "text":
            _merge_text(out, p["s"])
        elif p["t"] == "image":
            add_image(files.read_b64(p["f"]), p["f"]["mime"], p["f"]["name"])
        elif p["t"] == "pdf":
            f = p["f"]
            if (f.get("meta") or {}).get("scanned"):
                _merge_text(out, f"[扫描版 PDF：{f['name']}，以下是前几页的图像]")
                for i, pg in enumerate(files.pdf_page_images(f), 1):
                    add_image(base64.b64encode(pg.read_bytes()).decode(), "image/jpeg", f"{f['name']} 第 {i} 页")
            else:
                _merge_text(out, _file_text_block(f))
        elif p["t"] == "doc":
            _merge_text(out, _file_text_block(p["f"]))
    return out


def _codebuddy_error(result: dict) -> str:
    msg = str(result.get("result") or "；".join(str(e) for e in result.get("errors") or [])
              or result.get("subtype") or "未知错误")
    cats = {i.get("category") for i in result.get("errors_info") or [] if isinstance(i, dict)}
    if "auth" in cats or "Authentication required" in msg:
        CAPS["codebuddy"]["at"] = 0.0             # 下次取清单时重新问一遍登录状态
        msg = "CodeBuddy 还没登录，或者登录过期了：在服务器上运行 codebuddy，按提示登录一次。（原始报错：" + msg + "）"
    elif "quota" in cats:
        msg = "CodeBuddy 的额度不够，或者被限频了。（原始报错：" + msg + "）"
    return msg[:1500]


async def run_codebuddy(model: str, effort: str, system: str, parts: list[dict], web: bool, sink: Sink,
                        mcp=None) -> None:
    cap = CAPS["codebuddy"]
    if not cap["ok"]:
        raise BackendError("CodeBuddy 用不了：" + cap["why"])
    _mcp_ready(mcp)
    if mcp and "--mcp-config" not in cap["flags"]:
        raise BackendError("这个版本的 CodeBuddy 没有 --mcp-config 参数，接不了 MCP。请升级，或者先在设置里把 MCP 停用。")
    content = await asyncio.to_thread(parts_to_codebuddy, parts)
    builtin = (["WebSearch", "WebFetch"] if web else []) + (
        ["ViewImage"] if any(b.get("type") == "image" for b in content) else [])
    args = [config.CODEBUDDY_BIN, "-p", "--model", model, "--input-format", "stream-json",
            "--output-format", "stream-json", "--system-prompt", system, "--tools", ",".join(builtin)]
    args += _codebuddy_common()
    if "--include-partial-messages" in cap["flags"]:
        args.append("--include-partial-messages")
    mdir = None
    try:
        if mcp:
            mdir = config.RUNS_DIR / ("mcp-" + secrets.token_hex(8))
            mdir.mkdir(parents=True, mode=0o700)
            fd = os.open(mdir / "mcp.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(mcp.claude_config(), f)       # 配置文件的格式和 Claude Code 的一样
            args += ["--mcp-config", str(mdir / "mcp.json")]
        if builtin or mcp:
            # 一个名字一个参数：写成逗号连起来的一整串它不认（实测，抓网页会因为“没人批准”被拒）
            args += ["--allowedTools", *builtin, *(mcp.claude_allowed() if mcp else [])]
        if effort and "--effort" in cap["flags"]:
            args += ["--effort", effort]
        line = json.dumps({"type": "user", "message": {"role": "user", "content": content}}, ensure_ascii=False)
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(config.EMPTY_DIR), env=_codebuddy_env(mcp), start_new_session=True, limit=256 * 1024 * 1024)
    except BaseException:
        if mdir:
            shutil.rmtree(mdir, ignore_errors=True)
        raise
    errbuf: list[str] = []
    err_task = asyncio.create_task(_drain_stderr(proc, errbuf))
    feed_task = asyncio.create_task(_feed_stdin(proc, line.encode() + b"\n"))
    got_text = False
    got_thinking = False
    need_sep = False
    seen_tools: set[str] = set()
    result = None
    try:
        async for raw in proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "stream_event":
                e = ev.get("event") or {}
                et = e.get("type")
                if et == "content_block_start":
                    bt = (e.get("content_block") or {}).get("type")
                    if bt == "text" and got_text:
                        need_sep = True
                    elif bt == "thinking" and got_thinking:
                        sink.thinking("\n\n")
                elif et == "content_block_delta":
                    d = e.get("delta") or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        if need_sep:
                            sink.text("\n\n")
                            need_sep = False
                        sink.text(d["text"])
                        got_text = True
                    elif d.get("type") == "thinking_delta" and d.get("thinking"):
                        sink.thinking(d["thinking"])
                        got_thinking = True
            elif t == "assistant":
                for b in (ev.get("message") or {}).get("content") or []:
                    if b.get("type") == "tool_use" and b.get("id") not in seen_tools:
                        seen_tools.add(b.get("id"))
                        inp = b.get("input") or {}
                        name = str(b.get("name") or "")
                        if name == "WebSearch":
                            sink.activity({"kind": "search", "text": str(inp.get("query") or "")})
                        elif name == "WebFetch":
                            sink.activity({"kind": "fetch", "text": str(inp.get("url") or "")})
                        elif name == "ViewImage":
                            sink.activity({"kind": "tool", "text": "看图"})
                        elif name.startswith("mcp__"):
                            pass                  # MCP 调用由 mcp.RunCtx.call 记（带参数和结果）
                        else:
                            sink.activity({"kind": "tool", "text": name})
            elif t == "result":
                result = ev
        await proc.wait()
    finally:
        if mcp:
            mcp.closed = True
        await _kill(proc)
        for tk in (err_task, feed_task):
            tk.cancel()
        if mdir:
            shutil.rmtree(mdir, ignore_errors=True)

    if result is None:
        tail = "".join(errbuf)[-800:].strip()
        raise BackendError(f"codebuddy 没有返回结果（退出码 {proc.returncode}）{('：' + tail) if tail else ''}")
    u = result.get("usage") or {}
    # 它报的 input_tokens 已经把缓存命中的算在里面了（和 Claude Code 不一样）；花的是额度（credits），不是钱
    tot = ((result.get("_meta") or {}).get("codebuddy.ai/turnUsage") or {}).get("total") or {}
    cr = tot.get("credits")
    sink.usage({"in": u.get("input_tokens") or 0, "out": u.get("output_tokens") or 0,
                **({"credits": cr} if isinstance(cr, (int, float)) and not isinstance(cr, bool) else {})})
    if result.get("is_error"):
        raise BackendError(_codebuddy_error(result))
    if not got_text and result.get("result"):
        sink.text(str(result["result"]))


async def quick_codebuddy(prompt: str, model: str, timeout: float = 60) -> str:
    """一次性小任务（起标题）。不带任何工具。失败返回空串。"""
    if not CAPS["codebuddy"]["ok"]:
        return ""
    args = [config.CODEBUDDY_BIN, "-p", "--model", model, "--output-format", "json", "--tools", "",
            "--system-prompt", "你是一个只输出结果、不加任何解释的小工具。"] + _codebuddy_common()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=str(config.EMPTY_DIR), env=_codebuddy_env(), start_new_session=True)
    except OSError:
        return ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
        d = json.loads(out)
        d = d[-1] if isinstance(d, list) and d else d       # 有的版本给的是整串事件，最后一个才是结果
        return "" if d.get("is_error") else str(d.get("result") or "").strip()
    except Exception:
        return ""
    finally:
        await _kill(proc)


async def _codebuddy_ask() -> dict | None:
    """用 SDK 的控制请求问 CLI 两件事：登录的是谁（initialize）、现在能选哪些模型（get_available_models）。
    只是问它自己，不发模型请求、不花额度。→ {"account": …, "models": [CLI 给的原始模型说明]}，问不出来是 None。"""
    args = [config.CODEBUDDY_BIN, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--tools", ""] + _codebuddy_common()
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=str(config.EMPTY_DIR), env=_codebuddy_env(), start_new_session=True, limit=32 * 1024 * 1024)
    except OSError:
        return None
    got: dict = {}
    try:
        for rid, sub in (("rt-init", "initialize"), ("rt-models", "get_available_models")):
            proc.stdin.write(json.dumps({"type": "control_request", "request_id": rid,
                                         "request": {"subtype": sub}}).encode() + b"\n")
        await proc.stdin.drain()                  # 标准输入不关：关了它就当没有要做的事，直接退出
        async with asyncio.timeout(45):
            async for raw in proc.stdout:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                r = ev.get("response") if ev.get("type") == "control_response" else None
                if isinstance(r, dict) and r.get("request_id") in ("rt-init", "rt-models"):
                    got[r["request_id"]] = r.get("response") if r.get("subtype") == "success" else None
                    if len(got) == 2:
                        break
    except (TimeoutError, OSError):
        pass
    finally:
        await _kill(proc)
    init, lst = got.get("rt-init"), got.get("rt-models")
    if not isinstance(init, dict) and not isinstance(lst, dict):
        return None
    init, lst = init or {}, lst or {}
    # rawModels 带着完整说明（能不能看图、思考强度有哪几档、额度倍率）；旧版本没有这个请求，就只有名字
    models = lst.get("rawModels") or [{"id": m.get("modelId"), "name": m.get("name")} for m in lst.get("availableModels") or []
                                      if isinstance(m, dict)] or init.get("models") or []
    return {"account": init.get("account") or None, "models": models}


async def codebuddy_refresh(force: bool = False) -> None:
    """模型清单和登录状态。隔一阵才重新问一次；问不出来就留着上一次的。"""
    cap = CAPS["codebuddy"]
    if not cap["ok"] or (not force and time.time() - cap["at"] < _codebuddy_ttl()):
        return
    async with _cb_lock:
        if not force and time.time() - cap["at"] < _codebuddy_ttl():
            return
        r = await _codebuddy_ask()
        if not isinstance(r, dict):
            cap["at"] = time.time() - _codebuddy_ttl() + 60   # 一分钟后再试
            return
        ms = [m for m in r["models"] if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]]
        if ms:
            cap["models"] = ms
        cap.update(account=r["account"] or codebuddy_login_file(), asked=True, at=time.time())


def codebuddy_refresh_soon() -> None:
    """到点了就在后台重新问一遍，不让取模型清单的请求等它（问一次要两三秒）。"""
    cap = CAPS["codebuddy"]
    if cap["ok"] and not _cb_tasks and time.time() - cap["at"] >= _codebuddy_ttl():
        t = asyncio.create_task(codebuddy_refresh())
        _cb_tasks.add(t)
        t.add_done_callback(_cb_tasks.discard)


def codebuddy_login_file() -> bool:
    """交互登录（/login）之后登录信息落在这个目录里。只看有没有文件，不读内容。"""
    try:
        return any(Path(config.HOME, ".local/share/CodeBuddyExtension/Data/Public/auth").glob("*.info"))
    except OSError:
        return False


async def probe_codebuddy() -> None:
    cap = CAPS["codebuddy"]
    cap.update(ok=False, version="", flags=set(), models=[], account=None, asked=False, at=0.0)
    if not config.CODEBUDDY_BIN:
        cap["why"] = "没有找到 codebuddy 命令（没装 CodeBuddy Code，或者 ROUNDTABLE_CODEBUDDY_BIN=off）"
        return
    code, out = await _run_quick([config.CODEBUDDY_BIN, "--help"])
    if code != 0:
        cap["why"] = f"codebuddy --help 运行失败（它要 Node.js 18.20 以上）：{out.strip()[-200:]}"
        return
    flags = set(re.findall(r"(?<![\w-])--[a-zA-Z][a-zA-Z-]+", out))
    cap["flags"] = flags
    _, ver = await _run_quick([config.CODEBUDDY_BIN, "--version"])
    cap["version"] = ver.strip().splitlines()[-1][:60] if ver.strip() else ""
    need = {"--tools", "--system-prompt", "--input-format", "--output-format", "--setting-sources", "--strict-mcp-config"}
    if need - flags:
        cap["why"] = "CodeBuddy Code 版本太旧，缺少参数 " + " ".join(sorted(need - flags)) + "，请升级"
        return
    cap.update(ok=True, why="")      # 模型清单另外问（codebuddy_refresh），要好几秒，不让启动等它


# ---------- Codex ----------

CODEX_DISABLE = ["shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "skill_search", "browser_use",
                 "computer_use", "in_app_browser", "goals", "sleep_tool", "tool_suggest", "hooks"]


_SYS_DIRS = ("bin", "sbin", "lib", "lib32", "lib64", "libx32")
_ETC = ("ssl", "pki", "ca-certificates", "hosts", "nsswitch.conf", "passwd", "group", "localtime")


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _codex_launch() -> tuple[list[str], list[str]]:
    """→ (在沙箱里怎么启动 codex, 沙箱里除了 /usr 和 CODEX_HOME 还要只读挂上的目录)。

    独立安装版是一个可执行文件；npm 版是 node 脚本，要把 node 和包目录也带进去。
    """
    real = os.path.realpath(config.CODEX_BIN)
    binds: list[str] = []
    if "node_modules" in Path(real).parts:
        pkg = Path(real)
        while pkg.parent.name not in ("node_modules", "") and not pkg.parent.name.startswith("@"):
            pkg = pkg.parent
        if pkg.parent.name.startswith("@"):
            pkg = pkg.parent
        binds.append(str(pkg))
        node = shutil.which("node", path=config.cli_env()["PATH"])
        if node:
            node = os.path.realpath(node)
            if not _under(node, "/usr"):
                binds.append(str(Path(node).parent.parent))
            return [node, real], binds
        return [real], binds
    if not _under(real, "/usr") and not _under(real, os.path.realpath(config.CODEX_HOME)):
        # 一般把它所在的目录挂进去（旁边可能有它要用的辅助程序）；但如果那个目录就是家目录或更上层，只挂这一个文件
        d = str(Path(real).parent)
        binds.append(real if _under(os.path.realpath(config.HOME), d) else d)
    return [real], binds


def _bwrap_static(bwrap: str, binds: list[str]) -> list[str]:
    args = [bwrap, "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent",
            "--ro-bind", "/usr", "/usr"]
    for d in _SYS_DIRS:                         # /bin 之类：合并了 /usr 的发行版是软链接，照原样做
        p = "/" + d
        if os.path.islink(p):
            args += ["--symlink", os.readlink(p), p]
        elif os.path.isdir(p):
            args += ["--ro-bind", p, p]
    for name in _ETC:
        if os.path.exists("/etc/" + name):
            args += ["--ro-bind", "/etc/" + name, "/etc/" + name]
    if os.path.exists("/etc/resolv.conf"):      # 常常是个指到 /run 下面的软链接，挂它指向的那个文件
        args += ["--ro-bind", os.path.realpath("/etc/resolv.conf"), "/etc/resolv.conf"]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/run", "--dir", config.HOME]
    for b in binds:
        if not _under(b, "/usr") and not _under(b, config.CODEX_HOME):
            args += ["--ro-bind", b, b]
    # ~/.codex 要可写：登录令牌续期、模型清单缓存、画出来的图都在这里
    args += ["--bind", config.CODEX_HOME, config.CODEX_HOME]
    return args


_MCP_NAME = re.compile(r"[A-Za-z0-9_-]+")


def _codex_config_args(mcp=None) -> list[str]:
    """不让聊天里的模型碰到你给 Codex 配的 MCP 服务器（网页设置里接的那些另算，见 mcp.py）。

    默认整个不读 ~/.codex/config.toml（--ignore-user-config，登录照常）。
    如果你要它读（ROUNDTABLE_CODEX_USER_CONFIG=on，比如自定义了模型供应商），
    或者这个版本没有那个选项，就把里面的 MCP 服务器一个个关掉。
    """
    cap = CAPS["codex"]
    args = [f for f in cap["flags"] if f == "--ignore-rules"]
    if not config.CODEX_USER_CONFIG and "--ignore-user-config" in cap["flags"]:
        return args + ["--ignore-user-config"]
    path = Path(config.CODEX_HOME, "config.toml")
    if not path.is_file():
        return args
    try:
        servers = tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers") or {}
    except Exception as e:
        raise BackendError(f"读不懂 {path}（{e}），没法确认里面的 MCP 服务器都关掉了，所以不运行 Codex") from None
    for name in servers:
        if not _MCP_NAME.fullmatch(name):
            raise BackendError(f"{path} 里的 MCP 服务器名字 {name!r} 没法安全地在命令行上关掉，所以不运行 Codex")
        if mcp and name in mcp.servers:
            raise BackendError(f"{path} 里有一个 MCP 服务器也叫 {name}，和网页设置里接的重名了。请把其中一个改名。")
        args += ["-c", f"mcp_servers.{name}.enabled=false"]
    return args


def _codex_cmd(rundir: Path) -> list[str]:
    cap = CAPS["codex"]
    if cap["sandbox"] == "bwrap":
        return cap["wrap"] + ["--bind", str(rundir), "/work", "--chdir", "/work"] + cap["exe"]
    return cap["exe"]


async def probe_codex() -> None:
    cap = CAPS["codex"]
    cap.update(ok=False, version="", disable=[], flags=[], sandbox="off", note="", wrap=[], exe=[],
               builtin_models=[])
    if not config.CODEX_BIN:
        cap["why"] = "没有找到 codex 命令（没装 Codex CLI，或者 ROUNDTABLE_CODEX_BIN=off）"
        return
    code, out = await _run_quick([config.CODEX_BIN, "features", "list"])
    if code != 0:
        cap["why"] = f"codex features list 运行失败（版本太旧？）：{out.strip()[-200:]}"
        return
    known = {ln.split()[0] for ln in out.splitlines() if ln.split()}
    # 不认识的名字 codex 会直接报错退出，所以只传这个版本有的
    cap["disable"] = [f for f in CODEX_DISABLE if f in known]
    if "shell_tool" not in known:
        cap["why"] = "这个版本的 Codex 没有 shell_tool 开关，没法保证关掉它跑命令的能力，所以不启用"
        return
    _, helptext = await _run_quick([config.CODEX_BIN, "exec", "--help"])
    cap["flags"] = [f for f in ("--ignore-user-config", "--ignore-rules") if f in helptext]
    _, ver = await _run_quick([config.CODEX_BIN, "--version"])
    cap["version"] = ver.strip().splitlines()[0][:60] if ver.strip() else ""
    Path(config.CODEX_HOME).mkdir(parents=True, exist_ok=True)
    exe, binds = _codex_launch()
    cap["exe"] = exe
    if config.CODEX_SANDBOX != "off":
        bwrap = shutil.which("bwrap", path=config.cli_env()["PATH"])
        problem = "" if bwrap else "没有找到 bwrap（bubblewrap）"
        if bwrap:
            wrap = _bwrap_static(bwrap, binds)
            code, out = await _run_quick(wrap + ["--bind", str(config.EMPTY_DIR), "/work", "--chdir", "/work"]
                                         + exe + ["--version"])
            if code == 0:
                cap.update(sandbox="bwrap", wrap=wrap)
            else:
                problem = "bwrap 沙箱里启动 codex 失败：" + out.strip()[-300:]
        if problem and config.CODEX_SANDBOX == "bwrap":
            cap["why"] = problem + "（ROUNDTABLE_CODEX_SANDBOX=bwrap 要求必须有沙箱）"
            return
        if problem:
            cap["note"] = problem + "。现在没有外层沙箱，只靠 Codex 自己的只读模式和关掉命令行工具。"
            print("codex:", cap["note"], flush=True)
    else:
        cap["note"] = "外层沙箱被关掉了（ROUNDTABLE_CODEX_SANDBOX=off），只靠 Codex 自己的只读模式和关掉命令行工具。"
    cap["builtin_models"] = await _codex_builtin_models()
    cap.update(ok=True, why="")


async def _codex_builtin_models() -> list[dict]:
    """codex 自带的模型清单（不用登录、不发请求）。models_cache.json 还没有的时候拿它顶上；旧版本没有这个命令就是空的。"""
    code, out = await _run_quick([config.CODEX_BIN, "debug", "models"])
    start = out.find("{")                       # 前面可能有几行警告
    if code != 0 or start < 0:
        return []
    try:
        models = json.loads(out[start:]).get("models")
    except Exception:
        return []
    return [m for m in models if isinstance(m, dict) and m.get("slug")] if isinstance(models, list) else []


async def probe() -> None:
    for d in (config.FILES_DIR, config.RUNS_DIR, config.EMPTY_DIR):
        d.mkdir(parents=True, exist_ok=True)
    await asyncio.gather(probe_claude(), probe_codex(), probe_codebuddy())


def _codex_search_text(item: dict) -> str:
    a = item.get("action") or {}
    q = item.get("query") or a.get("query") or ""
    if not q and a.get("queries"):
        q = " | ".join(str(s) for s in a["queries"][:3])
    if not q and a.get("url"):
        q = a["url"]
    return q


_CODEX_CITE = re.compile("\ue200.*?\ue201", re.S)
_CODEX_IMG_STUB = re.compile(r"!\[[^\]]*\]\((?!https?://)[^)]*\)\n?")


def _codex_clean(text: str) -> str:
    """去掉 Codex 的内部引用标记和“![..](attachment)”这种图片占位符（图片另行附上）。"""
    text = _CODEX_CITE.sub("", text)
    text = _CODEX_IMG_STUB.sub("", text)
    return re.sub("[\ue200-\ue2ff]", "", text).rstrip()


async def run_codex(model: str, effort: str, system: str, parts: list[dict], web: bool, sink: Sink,
                    run_id: str, vision: bool = True, mcp=None) -> None:
    cap = CAPS["codex"]
    if not cap["ok"]:
        raise BackendError("Codex 用不了：" + cap["why"])
    _mcp_ready(mcp)
    rundir = config.RUNS_DIR / run_id
    rundir.mkdir(parents=True, exist_ok=True)
    prompt, images = await asyncio.to_thread(parts_to_flat, parts, vision)
    img_args: list[str] = []
    for i, (path, mime) in enumerate(images, 1):
        dst = rundir / f"img{i}{'.png' if mime == 'image/png' else '.jpg'}"
        shutil.copyfile(path, dst)
        img_args += ["-i", dst.name]
    args = _codex_cmd(rundir) + [
        "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
        "-c", "model_reasoning_summary=auto", "-c", f"web_search={'live' if web else 'disabled'}",
        "-c", "developer_instructions=" + json.dumps(system, ensure_ascii=False)]
    if model and model != "default":            # default = 让 codex 用它自己配置的模型
        args += ["-m", model]
    if effort:
        args += ["-c", f"model_reasoning_effort={effort}"]
    for feat in cap["disable"]:
        args += ["--disable", feat]
    env = _mcp_env(mcp)
    try:
        args += _codex_config_args(mcp)
        if mcp:
            # 运行令牌走环境变量（bwrap 会原样带进沙箱），不出现在任何进程的命令行里
            args += mcp.codex_args()
            env["RT_MCP_TOKEN"] = mcp.token
        args += img_args + ["-"]
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(rundir), env=env, start_new_session=True, limit=256 * 1024 * 1024)
    except BaseException:
        shutil.rmtree(rundir, ignore_errors=True)
        raise
    errbuf: list[str] = []
    err_task = asyncio.create_task(_drain_stderr(proc, errbuf))
    feed_task = asyncio.create_task(_feed_stdin(proc, prompt.encode()))
    thread_id = None
    got_text = False
    got_thinking = False
    done = False
    error = None
    try:
        async for raw in proc.stdout:
            raw = raw.strip()
            if not raw.startswith(b"{"):
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "thread.started":
                thread_id = ev.get("thread_id")
            elif t == "item.completed":
                item = ev.get("item") or {}
                it = item.get("type")
                if it == "agent_message" and item.get("text"):
                    txt = _codex_clean(item["text"])
                    if txt:
                        sink.text(("\n\n" if got_text else "") + txt)
                        got_text = True
                elif it == "reasoning" and item.get("text"):
                    sink.thinking(("\n\n" if got_thinking else "") + item["text"])
                    got_thinking = True
                elif it == "web_search":
                    sink.activity({"kind": "search", "text": _codex_search_text(item) or "联网查询"})
                elif it == "command_execution":
                    sink.activity({"kind": "tool", "text": str(item.get("command", ""))[:200]})
                elif it == "error" and item.get("message"):
                    error = item["message"]
            elif t == "turn.completed":
                done = True
                u = ev.get("usage") or {}
                sink.usage({"in": u.get("input_tokens") or 0, "cached": u.get("cached_input_tokens") or 0,
                            "out": u.get("output_tokens") or 0,
                            "reasoning": u.get("reasoning_output_tokens") or 0})
            elif t == "turn.failed":
                error = ((ev.get("error") or {}).get("message")) or "turn failed"
            elif t == "error":
                error = ev.get("message") or "error"
        await proc.wait()
        # 画的图没有事件，直接落在 ~/.codex/generated_images/<thread_id>/
        if thread_id and "/" not in thread_id and ".." not in thread_id:
            gdir = Path(config.CODEX_HOME, "generated_images", thread_id)
            if gdir.is_dir():
                for p in sorted(gdir.iterdir()):
                    if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                        sink.image(p.read_bytes(), p.name)
                shutil.rmtree(gdir, ignore_errors=True)
    finally:
        if mcp:
            mcp.closed = True                     # 先关门再杀进程：CLI 收到 SIGTERM 时会把没做完的工具调用重发一遍
        await _kill(proc)
        for tk in (err_task, feed_task):
            tk.cancel()
        shutil.rmtree(rundir, ignore_errors=True)

    if error and not done:
        raise BackendError(str(error)[:1500])
    if not done:
        tail = "".join(errbuf)[-800:].strip()
        raise BackendError(f"codex 没有正常结束（退出码 {proc.returncode}）{('：' + tail) if tail else ''}")


# ---------- OpenRouter ----------

def _or_merge_reasoning(acc: list[dict], items) -> None:
    """流里的 reasoning_details 是一块一块来的，拼回完整的条目（下一轮要原样带回去，否则有的模型不认）。"""
    for it in items or []:
        if not isinstance(it, dict):
            continue
        cur = None
        if it.get("index") is not None:
            cur = next((x for x in acc if x.get("index") == it["index"] and x.get("type") == it.get("type")), None)
        elif acc and acc[-1].get("type") == it.get("type") and acc[-1].get("index") is None:
            cur = acc[-1]
        if cur is None:
            acc.append(dict(it))
            continue
        for k, v in it.items():
            if k in ("text", "summary", "data") and isinstance(v, str) and isinstance(cur.get(k), str):
                cur[k] += v
            elif v is not None and cur.get(k) is None:
                cur[k] = v


async def run_openrouter(model: str, effort: str, system: str, parts: list[dict], web: bool, sink: Sink,
                         info: dict | None = None, mcp=None) -> None:
    key = config.openrouter_key()
    if not key:
        raise BackendError("没有配置 OPENROUTER_API_KEY")
    info = info or {}
    content = await asyncio.to_thread(parts_to_openrouter, parts, bool(info.get("vision", True)))
    if len(content) == 1 and content[0]["type"] == "text":
        content = content[0]["text"]
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": content}]
    base: dict = {"model": model, "stream": True, "usage": {"include": True}}
    if info.get("image_out"):
        base["modalities"] = ["image", "text"]
        messages = messages[1:]                           # 画图模型多数不吃 system
    if effort and effort in (info.get("efforts") or []):
        base["reasoning"] = {"effort": effort}
    if web:
        base["plugins"] = [{"id": "web"}]
    tools = mcp.openai_tools() if mcp else []
    headers = {"Authorization": f"Bearer {key}", "X-Title": "Roundtable"}
    seen_images: set[int] = set()
    total = {"in": 0, "out": 0, "reasoning": 0, "cost": 0.0}
    got = {"usage": False, "text": False, "thinking": False}
    timeout = httpx.Timeout(connect=20, read=600, write=120, pool=20)

    async def one_round(client: httpx.AsyncClient, payload: dict):
        """发一次请求、把流读完。返回 (这一轮的文字, 工具调用, reasoning_details)。"""
        text: list[str] = []
        calls: dict[int, dict] = {}
        details: list[dict] = []
        usage = None
        sep_text, sep_think = got["text"], got["thinking"]
        async with client.stream("POST", "https://openrouter.ai/api/v1/chat/completions",
                                 headers=headers, json=payload) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode("utf-8", "replace")
                try:
                    msg = json.loads(body).get("error", {}).get("message") or body
                except ValueError:
                    msg = body
                raise BackendError(f"OpenRouter {r.status_code}：{str(msg)[:1200]}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ch = json.loads(data)
                except ValueError:
                    continue
                if ch.get("error"):
                    e = ch["error"]
                    raise BackendError(f"OpenRouter：{str(e.get('message') or e)[:1200]}")
                if ch.get("usage"):
                    usage = ch["usage"]
                for c in ch.get("choices") or []:
                    d = c.get("delta") or {}
                    if d.get("reasoning"):
                        sink.thinking(("\n\n" if sep_think else "") + d["reasoning"])
                        sep_think, got["thinking"] = False, True
                    if d.get("content"):
                        sink.text(("\n\n" if sep_text else "") + d["content"])
                        sep_text, got["text"] = False, True
                        text.append(d["content"])
                    _or_merge_reasoning(details, d.get("reasoning_details"))
                    for tc in d.get("tool_calls") or []:
                        if not isinstance(tc, dict):
                            continue
                        cur = calls.setdefault(tc.get("index") or 0, {"id": "", "name": "", "args": ""})
                        fn = tc.get("function") or {}
                        cur["id"] = cur["id"] or tc.get("id") or ""
                        cur["name"] = cur["name"] or fn.get("name") or ""
                        cur["args"] += fn.get("arguments") or ""
                    for im in d.get("images") or []:
                        url = (im.get("image_url") or {}).get("url") or ""
                        if url.startswith("data:") and "," in url:
                            h = hash(url)
                            if h in seen_images:
                                continue
                            seen_images.add(h)
                            try:
                                sink.image(base64.b64decode(url.split(",", 1)[1]))
                            except Exception:
                                pass
                    for ann in d.get("annotations") or []:
                        uc = ann.get("url_citation") or {}
                        if ann.get("type") == "url_citation" and uc.get("url"):
                            sink.activity({"kind": "cite", "text": uc.get("title") or uc["url"], "url": uc["url"]})
                    if c.get("finish_reason") == "error":
                        raise BackendError("OpenRouter：上游模型返回错误")
        if usage:
            det = usage.get("completion_tokens_details") or {}
            got["usage"] = True
            total["in"] += usage.get("prompt_tokens") or 0
            total["out"] += usage.get("completion_tokens") or 0
            total["reasoning"] += det.get("reasoning_tokens") or 0
            total["cost"] += usage.get("cost") or 0
        return "".join(text), [calls[i] for i in sorted(calls)], details

    async def call_tool(c: dict) -> str:
        slug_x = mcp.fn.get(c["name"])
        if not slug_x:
            return "没有这个工具。"
        try:
            args = json.loads(c["args"]) if c["args"].strip() else {}
        except ValueError:
            return "工具参数不是合法的 JSON，没有调用。请重新给出参数。"
        if not isinstance(args, dict):
            return "工具参数必须是一个 JSON 对象，没有调用。"
        res = await mcp.call(*slug_x, args)
        return res["content"][0]["text"]

    rounds = config.MCP_MAX_ROUNDS if tools else 1
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for rnd in range(rounds):
                payload = {**base, "messages": messages}
                if tools:
                    payload["tools"] = tools
                    if rnd == rounds - 1:
                        payload["tool_choice"] = "none"       # 最后一轮只许作答
                text, calls, details = await one_round(client, payload)
                if not calls or not mcp:
                    break
                if rnd == rounds - 1:
                    sink.text(("\n\n" if got["text"] else "") + f"（模型和工具已经来回 {rounds} 轮，到上限了，先停在这里。）")
                    break
                for i, c in enumerate(calls):
                    c["id"] = c["id"] or f"call_{rnd}_{i}"
                msg = {"role": "assistant", "content": text or None, "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                    for c in calls]}
                if details:
                    msg["reasoning_details"] = details
                messages.append(msg)
                results = await asyncio.gather(*(call_tool(c) for c in calls))
                messages += [{"role": "tool", "tool_call_id": c["id"], "content": res} for c, res in zip(calls, results)]
    finally:
        if got["usage"]:
            sink.usage({"in": total["in"], "out": total["out"], "reasoning": total["reasoning"],
                        "cost": total["cost"] if total["cost"] else None})


async def openrouter_key_info() -> dict | None:
    key = config.openrouter_key()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})
            d = r.json().get("data") or {}
            return {"limit": d.get("limit"), "usage": d.get("usage"), "remaining": d.get("limit_remaining"),
                    "label": d.get("label")}
    except Exception:
        return None


async def quick_openrouter(prompt: str, model: str, timeout: float = 60) -> str:
    """一次性小任务（起标题）走 OpenRouter。失败返回空串。"""
    key = config.openrouter_key()
    if not key:
        return ""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post("https://openrouter.ai/api/v1/chat/completions",
                             headers={"Authorization": f"Bearer {key}", "X-Title": "Roundtable"},
                             json={"model": model, "max_tokens": 200,
                                   "messages": [{"role": "user", "content": prompt}]})
            return ((r.json()["choices"][0]["message"].get("content")) or "").strip()
    except Exception:
        return ""


def codex_logged_in() -> bool:
    """只看有没有登录文件或密钥，不读内容。"""
    return (Path(config.CODEX_HOME, "auth.json").is_file()
            or any(n in config.CLI_PASS_ENV and os.environ.get(n) for n in ("OPENAI_API_KEY", "CODEX_API_KEY")))


def status() -> dict:
    """给前端看的后端状态。"""
    c, x, b = CAPS["claude"], CAPS["codex"], CAPS["codebuddy"]
    return {
        "claude": {"ok": c["ok"], "why": c["why"], "version": c["version"]},
        "codex": {"ok": x["ok"], "why": x["why"], "version": x["version"], "sandbox": x["sandbox"],
                  "note": x["note"], "logged_in": codex_logged_in() if x["ok"] else False},
        # logged_in：None = 还没问出来（别乱报“没登录”）
        "codebuddy": {"ok": b["ok"], "why": b["why"], "version": b["version"], "models": len(b["models"]),
                      "found": bool(config.CODEBUDDY_BIN),      # 没装的话界面上提都不提它
                      "logged_in": (bool(b["account"]) if b["asked"] else None) if b["ok"] else False},
        "openrouter": {"ok": bool(config.openrouter_key()),
                       "why": "" if config.openrouter_key() else "没有配置 OPENROUTER_API_KEY"},
    }
