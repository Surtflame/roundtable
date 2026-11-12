"""别的机器上的“环境”（envs.py 里的清单）：经 SSH 在那台机器的 tmux 里开窗口、接进去、列那边的历史对话。

那边用的也是单独的套接字（tmux -L <config.TMUX_SOCKET>），和那台机器上别的 tmux（比如它自己的 Remote Control 服务）分开。
窗口活在那台机器上：这边的网页服务、这台机器的 tmux 重启都不影响它。
这边只在自己的 tmux 里留一条占位记录（@kind=remote，@env=环境名，会话名和那边的一样），窗口清单靠它，不用每次都连过去问。

所有命令都是参数列表拼出来再整体加引号的，不拼接浏览器给的字符串；环境名只认 清单里有的。
SSH 连接是复用的（ControlMaster）：跨洲的机器握手一次要好几秒，复用之后每条命令只要一个来回。
"""
import asyncio
import json
import os
import secrets
import shlex
import time
from pathlib import Path

from . import config, envs, tmuxctl

REMOTE_CONF = ".config/roundtable/tmux.conf"
SCAN_TTL = 45                     # 那边的历史对话多久重新扫一次（秒）
_ready: set[str] = set()
_scan: dict[str, dict] = {}       # 环境名 → {"at": 时间, "sessions": [...], "procs": [...], "task": 正在扫的任务, "error": 上次失败原因}


class RemoteError(tmuxctl.TmuxError):
    pass


def env(key: str) -> dict:
    e = envs.get(key)
    if not e:
        raise RemoteError("没有这个环境")
    return e


def ssh_argv(key: str, tty: bool = False) -> list[str]:
    e = env(key)
    return ["ssh", "-tt" if tty else "-T", "-i", e["key"], "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=12", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
            "-o", "ControlMaster=auto", "-o", f"ControlPath={config.SSH_CTL_DIR}/ssh-%C", "-o", "ControlPersist=900",
            *(["-p", str(e["port"])] if e.get("port") else []), e["ssh"]]


def _local_env() -> dict:
    return {k: v for k, v in tmuxctl.env().items() if k != "TMUX_TMPDIR"}


async def run(key: str, *argv: str, stdin: bytes | None = None, timeout: float = 30, check: bool = True) -> str:
    """在那台机器上跑一条命令（参数列表），→ 标准输出。"""
    proc = await asyncio.create_subprocess_exec(
        *ssh_argv(key), shlex.join(argv), stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_local_env())
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RemoteError(f"连 {env(key)['label']} 超时了") from None
    if proc.returncode == 255:
        raise RemoteError(f"连不上 {env(key)['label']}：{err.decode('utf-8', 'replace').strip()[-200:]}")
    if proc.returncode != 0 and check:
        raise RemoteError(err.decode("utf-8", "replace").strip()[-300:] or f"退出码 {proc.returncode}")
    return out.decode("utf-8", "replace")


def _tmux(*args: str) -> list[str]:
    return ["tmux", "-L", config.TMUX_SOCKET, *args]


async def ensure(key: str) -> None:
    """那边的 tmux 配置和服务：每次网页服务启动后第一次用到时放一遍（配置文件和这边用的是同一份）。"""
    mark = f"{key}:{env(key)['ssh']}"                # 同一个名字换了机器要重新放
    if mark in _ready:
        return
    conf = Path(config.TMUX_CONF).read_bytes()
    await run(key, "sh", "-c", f'mkdir -p "$HOME/{os.path.dirname(REMOTE_CONF)}" && cat > "$HOME/{REMOTE_CONF}"', stdin=conf)
    # 服务已经在跑的话 -f 不起作用，所以再 source 一遍
    await run(key, "sh", "-c", f'tmux -L {shlex.quote(config.TMUX_SOCKET)} -f "$HOME/{REMOTE_CONF}" start-server \\; source-file "$HOME/{REMOTE_CONF}"')
    _ready.add(mark)


async def create(key: str, kind: str, title: str = "", agent: str = "", session_id: str = "", rc: bool = False,
                 cwd: str = "") -> str:
    """在那台机器上开一个窗口，→ 窗口名（这边的占位记录也叫这个名字）。"""
    e = env(key)
    if kind == "agent" and agent != "claude":
        raise RemoteError("那台机器上只装了 Claude Code")
    if session_id and not tmuxctl.ID_RE.match(session_id):
        raise RemoteError("对话 id 的格式不对")
    await tmuxctl.ensure_server()
    if len(await tmuxctl.list_windows()) >= config.MAX_WINDOWS:
        raise RemoteError(f"最多同时开 {config.MAX_WINDOWS} 个窗口，先关掉几个")
    await ensure(key)
    name = "w" + secrets.token_hex(4)
    cwd = cwd if cwd.startswith("/") and "\x00" not in cwd and "\n" not in cwd else e["cwd"]
    args = ["new-session", "-d", "-s", name, "-c", cwd, "-x", "160", "-y", "45"]
    if kind == "agent":
        args += ["bash", "-lc", tmuxctl.agent_command(agent, session_id or None, rc)]
    await run(key, *_tmux(*args))
    await tmuxctl.placeholder(name, {"@title": title, "@kind": "remote", "@agent": agent if kind == "agent" else "",
                                     "@asid": session_id if kind == "agent" else "", "@env": key})
    return name


async def alive(key: str, name: str) -> bool:
    tmuxctl._need(name)
    try:
        await run(key, *_tmux("has-session", "-t", f"={name}"))
        return True
    except RemoteError as e:
        if "连不上" in str(e) or "超时" in str(e):
            raise
        return False


async def kill(key: str, name: str) -> None:
    tmuxctl._need(name)
    await run(key, *_tmux("kill-session", "-t", f"={name}"), check=False)


def attach_argv(key: str, name: str) -> list[str]:
    tmuxctl._need(name)
    return ["setsid", "-c", *ssh_argv(key, tty=True), shlex.join(_tmux("attach-session", "-t", f"={name}"))]


# ---------- 那边的历史对话 ----------

async def _do_scan(key: str) -> None:
    c = _scan.setdefault(key, {"at": 0, "sessions": [], "procs": []})
    try:
        e = env(key)
        src = (Path(__file__).parent / "scan.py").read_bytes()
        d = json.loads(await run(key, "python3", "-", e["home"], stdin=src, timeout=40))
        for s in d["sessions"]:
            s["env"] = key
        for p in d["procs"]:
            p["env"] = key
        c.update(at=time.time(), sessions=d["sessions"], procs=d["procs"], error="")
    except (RemoteError, ValueError, KeyError, OSError) as ex:
        c.update(at=time.time(), error=str(ex)[:200])
    finally:
        c["task"] = None


def scan_cached(key: str, refresh: bool = True) -> dict:
    """那边的会话和进程（上次扫到的）。过期了就在后台重新扫一遍，这次先用旧的——不让列表等 SSH。"""
    c = _scan.setdefault(key, {"at": 0, "sessions": [], "procs": []})
    if refresh and time.time() - c["at"] > SCAN_TTL and not c.get("task"):
        c["task"] = asyncio.get_running_loop().create_task(_do_scan(key))
    return c


def invalidate(key: str) -> None:
    c = _scan.get(key)
    if c:
        c["at"] = 0
