"""一条 WebSocket ↔ 一个伪终端里的 `tmux attach`。

浏览器 → 服务器：二进制帧 = 键盘输入；文本帧 = JSON（{"resize":[列,行]}）。
服务器 → 浏览器：二进制帧 = 终端输出。
连接断了只是 tmux 的一个客户端走了，窗口里的程序照常跑。
"""
import asyncio
import fcntl
import json
import os
import pty
import struct
import termios
import time

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from . import auth, config, remote, tmuxctl

CLOSE_AUTH = 4401          # 登录没了（会话过期、被撤销）
CLOSE_LOCKED = 4423        # 要重新输验证码
CLOSE_GONE = 4404          # 窗口已经不在了
LIVE: set = set()          # 现在连着的终端（锁定、退出时一起断开）


def _winsize(fd: int, cols: int, rows: int) -> None:
    cols, rows = max(20, min(int(cols), 500)), max(5, min(int(rows), 300))
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def _write_all(loop, fd: int, data: bytes) -> None:
    while data:
        try:
            n = os.write(fd, data)
            data = data[n:]
        except BlockingIOError:
            fut = loop.create_future()
            loop.add_writer(fd, lambda: fut.done() or fut.set_result(None))
            try:
                await fut
            finally:
                loop.remove_writer(fd)


async def serve(ws: WebSocket, name: str, cols: int, rows: int, unlock_hash: str, sid: str | None, email: str) -> None:
    """ws 已经 accept 过了。"""
    loop = asyncio.get_running_loop()
    where, tgt = await tmuxctl.target(name)         # 终端实际在哪：我们的 tmux / 默认 tmux 里借来的会话 / 别的机器
    try:
        gone = (where == "ext" and not await tmuxctl.ext_alive(tgt)) or (where == "remote" and not await remote.alive(tgt, name))
    except tmuxctl.TmuxError:                       # 那台机器现在连不上：窗口留着，浏览器过一会儿会自己再试
        return await ws.close(1013)
    if gone:                                        # 那边的会话已经没了：这条占位记录也不用留
        await tmuxctl.kill(name)
        return await ws.close(CLOSE_GONE)
    argv = (tmuxctl.attach_argv(name, tgt) if where == "ext" else remote.attach_argv(tgt, name) if where == "remote"
            else tmuxctl.attach_argv(name))
    master, slave = pty.openpty()
    try:
        _winsize(master, cols, rows)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=slave, stdout=slave, stderr=slave, env=tmuxctl.env(), cwd=config.HOME)
    except BaseException:
        os.close(master)
        os.close(slave)
        raise
    os.close(slave)
    os.set_blocking(master, False)
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    reading = True
    eof = False

    def on_readable() -> None:
        nonlocal reading, eof
        try:
            data = os.read(master, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(master)
            reading, eof = False, True
            q.put_nowait(None)
            return
        q.put_nowait(data)
        if q.full():                                # 浏览器收得慢：先不读了，等队列腾出地方
            loop.remove_reader(master)
            reading = False

    loop.add_reader(master, on_readable)
    entry = (ws, unlock_hash)
    LIVE.add(entry)

    async def pump_out() -> None:
        nonlocal reading
        while True:
            data = await q.get()
            if data is None:
                return
            await ws.send_bytes(data)
            if not reading and not eof and not q.full():
                reading = True
                loop.add_reader(master, on_readable)

    async def pump_in() -> None:
        touched = 0.0
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes") is not None:
                await _write_all(loop, master, msg["bytes"])
                if time.time() - touched > 20:      # 有按键 = 有人在用，闲置计时从头算
                    touched = time.time()
                    auth.unlock_touch(unlock_hash)
            elif msg.get("text"):
                try:
                    d = json.loads(msg["text"])
                    if isinstance(d.get("resize"), list) and len(d["resize"]) == 2:
                        _winsize(master, d["resize"][0], d["resize"][1])
                except (ValueError, TypeError, OSError):
                    pass

    async def recheck() -> int:
        """连上之后登录检查不会再自动发生，所以这里隔一阵自己查一次。返回要用的关闭码。"""
        while True:
            await asyncio.sleep(config.REVALIDATE_EVERY)
            if auth.session_user(sid) != email:
                return CLOSE_AUTH
            if not auth.unlock_alive(unlock_hash):
                return CLOSE_LOCKED

    tasks = [asyncio.create_task(c) for c in (pump_out(), pump_in(), recheck())]
    code = 1000
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is tasks[2] and not t.cancelled() and t.exception() is None:
                code = t.result()
            elif t is tasks[0] and t.exception() is None:
                try:
                    if where == "ext" and not await tmuxctl.ext_alive(tgt) and await tmuxctl.exists(name):
                        await tmuxctl.kill(name)
                    elif where == "remote" and not await remote.alive(tgt, name) and await tmuxctl.exists(name):
                        await tmuxctl.kill(name)
                except tmuxctl.TmuxError:
                    pass
                code = CLOSE_GONE if not await tmuxctl.exists(name) else 1000
    except WebSocketDisconnect:
        pass
    finally:
        LIVE.discard(entry)
        for t in tasks:
            t.cancel()
        try:
            loop.remove_reader(master)
        except (ValueError, OSError):
            pass
        os.close(master)                            # tmux 客户端收到挂断，自己退出
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            proc.kill()
        if ws.application_state == WebSocketState.CONNECTED:
            try:
                await ws.close(code)
            except RuntimeError:
                pass


async def close_all(unlock_hash: str | None = None, code: int = CLOSE_LOCKED) -> None:
    """断开终端连接：给了 unlock_hash 就只断那个解锁会话的，不给就全断。"""
    for ws, h in list(LIVE):
        if unlock_hash is None or h == unlock_hash:
            try:
                await ws.close(code)
            except Exception:  # noqa: BLE001
                pass
