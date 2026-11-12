"""管 tmux：列窗口、开窗口、改名、关窗口。

网页上的一个“窗口”= tmux（套接字 config.TMUX_SOCKET）里的一个会话，会话名是我们起的 w+十六进制，
显示用的名字、种类、对应的对话 id 存在会话自己的选项里（@title / @kind / @agent / @asid），
这样从 SSH 用 tmux -L web 接进来看到的是同一份，网页这边也不用另外记账。

还有一种“借来的”窗口（@kind=ext）：对话本来就跑在这台机器默认的那个 tmux 里（不带 -L 的，比如你自己开的——
带着 Remote Control 的 Claude Code），网页这边不另起进程，直接接到那个会话上看、接着聊，和 Claude 应用里是同一个进程。
这种窗口在我们的 tmux 里只留一条占位的记录（里面是个空转的 tail），@ext 记着接的是哪个会话；关掉它只是不看了，那边照常跑。
函数里 ext=True 的意思是“对默认的那个 tmux 说话”。
"""
import asyncio
import os
import re
import secrets
import shlex

from . import config, envs

NAME_RE = re.compile(r"^w[0-9a-f]{6,12}$")
ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{7,63}$")
EXT_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_.-]{0,39}$")     # 默认 tmux 里会话的名字（借来的窗口接的目标）
SEP = "<|>"                       # 不能用控制字符：tmux 输出时会把它们转义掉
FIELDS = ["session_name", "@title", "@kind", "@agent", "@asid", "@ext", "@env", "session_created", "session_activity",
          "session_attached", "pane_current_command", "pane_current_path", "pane_dead"]


class TmuxError(Exception):
    pass


def base(ext: bool = False) -> list[str]:
    return ["tmux"] if ext else ["tmux", "-L", config.TMUX_SOCKET]


def _down(e: Exception) -> bool:
    return any(k in str(e) for k in ("no server running", "error connecting", "No such file"))


def env() -> dict:
    e = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL")}
    e.update(HOME=config.HOME, TERM="xterm-256color")
    if config.TMUX_TMPDIR:
        e["TMUX_TMPDIR"] = config.TMUX_TMPDIR
    e.setdefault("LANG", "C.UTF-8")
    e.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return e


async def run(*args: str, check: bool = True, ext: bool = False) -> str:
    proc = await asyncio.create_subprocess_exec(
        *base(ext), *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env())
    out, err = await asyncio.wait_for(proc.communicate(), 10)
    if proc.returncode != 0 and check:
        raise TmuxError(err.decode("utf-8", "replace").strip() or f"tmux 退出码 {proc.returncode}")
    return out.decode("utf-8", "replace")


async def server_up() -> bool:
    try:
        await run("list-sessions", "-F", "x")
        return True
    except TmuxError as e:
        return "no server running" not in str(e) and "No such file" not in str(e) and "error connecting" not in str(e)


async def ensure_server() -> None:
    """tmux 服务没起来就自己起一个。用 systemd 部署时建议另装 deploy/roundtable-tmux.service（见 config.TMUX_AUTOSTART 的说明）。"""
    if await server_up():
        return
    if not config.TMUX_AUTOSTART:
        raise TmuxError("tmux 服务没起来")
    proc = await asyncio.create_subprocess_exec(
        *base(), "-f", str(config.TMUX_CONF), "start-server", stdin=asyncio.subprocess.DEVNULL, env=env(),
        start_new_session=True)
    await proc.wait()


def _clean(s: str, n: int = 80) -> str:
    return " ".join(str(s).replace(SEP, " ").split())[:n]


async def list_windows() -> list[dict]:
    try:
        out = await run("list-sessions", "-F", SEP.join("#{%s}" % f for f in FIELDS))
    except TmuxError as e:
        if _down(e):
            raise TmuxError("tmux 服务没起来（没装 tmux？）") from None
        raise
    ws = []
    for line in out.splitlines():
        v = line.split(SEP)
        if len(v) != len(FIELDS) or not NAME_RE.match(v[0]):
            continue                                # 不是我们开的会话（有人从 SSH 手工建的）：不碰
        d = dict(zip(FIELDS, v))
        remote = d["@kind"] == "remote" and d["@env"] in envs.all()
        if remote:                                   # 占位记录里的程序和目录没有意义，换成那边的
            d["pane_current_command"], d["pane_current_path"] = d["@agent"] or "shell", envs.all()[d["@env"]]["cwd"]
        ws.append({"name": d["session_name"], "title": d["@title"], "kind": d["@kind"] or "shell",
                   "agent": d["@agent"], "asid": d["@asid"], "ext": d["@ext"] if d["@kind"] == "ext" else "",
                   "env": d["@env"] if remote else "", "created": int(d["session_created"] or 0),
                   "activity": int(d["session_activity"] or 0), "attached": int(d["session_attached"] or 0),
                   "command": d["pane_current_command"], "cwd": d["pane_current_path"],
                   "dead": d["pane_dead"] == "1"})
    ws.sort(key=lambda w: (w["created"], w["name"]))
    return ws


def agent_command(agent: str, session_id: str | None, rc: bool = False, prompt_file: str = "", first: str = "") -> str:
    """要在新窗口里跑的命令。跑完（或者没装）回到一个登录 shell，窗口不会跟着消失。
    rc：Claude Code 带上 Remote Control，这个对话在 Claude 应用（手机、桌面）里也能看到、接着聊。
    prompt_file：把这个文件的内容加进 Claude Code 的系统提示，再用 first 当第一句话把它叫起来（都是服务器自己写的，不是浏览器给的）。"""
    if agent == "claude":
        cmd = "claude" + (f" --resume {shlex.quote(session_id)}" if session_id else "") + (" --remote-control" if rc else "")
        if prompt_file:
            # 任务说明放进系统提示（不占屏幕），第一句话只是把它叫起来
            cmd += f' --append-system-prompt "$(cat {shlex.quote(prompt_file)})" {shlex.quote(first or "开始吧。")}'
    elif agent == "codex":
        cmd = "codex" + (f" resume {shlex.quote(session_id)}" if session_id else "")
    else:
        raise TmuxError("不认识的种类")
    return f"{cmd}; printf '\\n[%s 已退出，现在是普通的 shell]\\n' {shlex.quote(agent)}; exec \"${{SHELL:-bash}}\" -l"


async def create(kind: str, cwd: str, title: str = "", agent: str = "", session_id: str = "", rc: bool = False,
                 prompt_file: str = "", first: str = "") -> str:
    """kind: shell / agent（agent=claude|codex，session_id 给了就是接着那个对话）。→ 窗口名。"""
    await ensure_server()
    if len(await list_windows()) >= config.MAX_WINDOWS:
        raise TmuxError(f"最多同时开 {config.MAX_WINDOWS} 个窗口，先关掉几个")
    if not os.path.isdir(cwd):
        raise TmuxError(f"目录不存在：{cwd}")
    if session_id and not ID_RE.match(session_id):
        raise TmuxError("对话 id 的格式不对")
    name = "w" + secrets.token_hex(4)
    args = ["new-session", "-d", "-s", name, "-c", cwd, "-x", "160", "-y", "45"]
    if kind == "agent":
        # 用登录 shell 起，PATH 里才有 ~/.local/bin
        args += ["bash", "-lc", agent_command(agent, session_id or None, rc, prompt_file, first)]
    await run(*args)
    opts = {"@title": _clean(title), "@kind": "agent" if kind == "agent" else "shell",
            "@agent": agent if kind == "agent" else "", "@asid": session_id if kind == "agent" else ""}
    for k, v in opts.items():
        await run("set-option", "-t", f"={name}:", k, v)       # 设选项要写成 =名字: 才认
    return name


async def panes(ext: bool = False) -> dict[int, str]:
    """窗格里直接跑着的那个进程的 pid → 它所在的会话名。服务没起来就是空的。"""
    try:
        out = await run("list-panes", "-a", "-F", "#{pane_pid}" + SEP + "#{session_name}", ext=ext)
    except TmuxError as e:
        if _down(e) or "no current target" in str(e):     # 服务没起来，或者起来了但一个会话都没有
            return {}
        raise
    res = {}
    for line in out.splitlines():
        pid, _, name = line.partition(SEP)
        if pid.isdigit():
            res[int(pid)] = name
    return res


async def create_ext(target: str, title: str = "") -> str:
    """借一个窗口：接到默认 tmux 里的 target 会话上。→ 我们这边那条占位记录的窗口名。"""
    await ensure_server()
    if not EXT_RE.match(target or ""):
        raise TmuxError("会话名不对")
    ws = await list_windows()
    for w in ws:
        if w["ext"] == target:
            return w["name"]
    if len(ws) >= config.MAX_WINDOWS:
        raise TmuxError(f"最多同时开 {config.MAX_WINDOWS} 个窗口，先关掉几个")
    await run("has-session", "-t", f"={target}", ext=True)
    # 那边的会话按我们的习惯调一下：滚轮能翻历史、拖选能复制；状态栏收起来（窗口列表在网页上）。只动这一个会话的选项
    for k, v in (("mouse", "on"), ("status", "off")):
        await run("set-option", "-t", f"={target}:", k, v, ext=True, check=False)
    name = "w" + secrets.token_hex(4)
    await placeholder(name, {"@title": title, "@kind": "ext", "@agent": "claude", "@asid": "", "@ext": target})
    return name


async def placeholder(name: str, opts: dict) -> None:
    """一条占位记录：真正的终端在别处（默认 tmux 的某个会话，或者别的机器上），这里只记它是谁、接到哪。"""
    _need(name)
    await run("new-session", "-d", "-s", name, "-c", config.HOME, "-x", "80", "-y", "24", "tail", "-f", "/dev/null")
    for k, v in opts.items():
        await run("set-option", "-t", f"={name}:", k, _clean(v) if k == "@title" else str(v))


async def target(name: str) -> tuple[str, str]:
    """这个窗口的终端实际在哪：("local", "") 我们自己的 tmux / ("ext", 会话名) 默认 tmux 里借来的 / ("remote", 环境名) 别的机器上。"""
    _need(name)
    out = await run("display-message", "-p", "-t", f"={name}:", "#{@kind}" + SEP + "#{@ext}" + SEP + "#{@env}", check=False)
    kind, _, rest = out.strip().partition(SEP)
    ext, _, envk = rest.partition(SEP)
    if kind == "ext" and EXT_RE.match(ext):
        return "ext", ext
    if kind == "remote" and envk in envs.all():
        return "remote", envk
    return "local", ""


async def ext_alive(target: str) -> bool:
    try:
        await run("has-session", "-t", f"={target}", ext=True)
        return True
    except TmuxError:
        return False


async def rename(name: str, title: str) -> None:
    _need(name)
    await run("set-option", "-t", f"={name}:", "@title", _clean(title))


async def kill(name: str) -> None:
    _need(name)
    await run("kill-session", "-t", f"={name}")


async def exists(name: str) -> bool:
    _need(name)
    try:
        await run("has-session", "-t", f"={name}")
        return True
    except TmuxError:
        return False


def _need(name: str) -> None:
    if not NAME_RE.match(name or ""):
        raise TmuxError("窗口名不对")


def attach_argv(name: str, ext: str = "") -> list[str]:
    _need(name)
    # setsid -c：给它一个自己的会话，并把这个伪终端设成控制终端（改窗口大小的信号才送得到）
    if ext:
        # 借来的窗口：直接接默认 tmux 里的那个会话。那边没有我们的配置，真彩色和剪贴板在这里用 -T 声明
        return ["setsid", "-c", *base(True), "-T", "RGB,clipboard", "attach-session", "-t", f"={ext}"]
    return ["setsid", "-c", *base(), "attach-session", "-t", f"={name}"]
