"""扫 Claude Code 自己留在磁盘上的东西：会话存档（标题、目录、第一句话、是人开的还是程序开的）和正在跑的进程。

这个文件只用标准库、不引用项目里别的模块，因为它有两种用法：
  - 本机：history.py 直接 import
  - 别的机器（配置里的“环境”）：整个文件经 SSH 喂给那边的 python3 当脚本跑，结果用 JSON 打回来（见文件末尾和 remote.py）
只读，不改任何东西。
"""
import calendar
import glob
import json
import mmap
import os
import re
import sys
import time

HEAD = 256 * 1024
N_TITLE_C, N_TITLE_A, N_ENQ = b'"type":"custom-title"', b'"type":"ai-title"', b'"operation":"enqueue"'
N_BRIDGE = b'"type":"bridge-session"'
# 人发的消息，Claude Code 会在那条记录上标 origin.kind = human（在应用里发的、在终端里敲的都算）；
# 程序调的 claude -p 没有这个标记。老版本不写 origin，所以再认一条：在终端界面里开的（entrypoint = cli）也算人开的
N_HUMAN, N_CLI = b'"origin":{"kind":"human"', b'"entrypoint":"cli"'
_TS = re.compile(rb'"timestamp":"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)')
# 路径 → 上次扫到的结果。存档只往后追加，所以文件变大时只扫新长出来的那一截（有的存档几十 MB，而且正开着的对话一直在长）
_cache: dict[str, dict] = {}


def _first_text(d: dict) -> str:
    """一条 user 记录里人打的字。工具结果、系统塞进去的东西不算。"""
    if d.get("isMeta") or d.get("isSidechain") or d.get("isCompactSummary"):
        return ""
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str):
        return ""
    c = c.strip()
    return "" if not c or c.startswith(("<", "Caveat:", "[Request interrupted")) else c


_CWD = re.compile(rb'"cwd":"((?:[^"\\]|\\.)*)"')
HEAD_MAX = 8 * 1024 * 1024


def _head(path: str) -> dict:
    """文件开头：在哪个目录开的、第一句话是什么。
    一般前 256K 里就有；但存档被压缩重写过之后，开头可能是一条带图片的几百 K 的长行，所以按行往下读，
    找到目录和第一句话就停，最多读 HEAD_MAX。"""
    info = {"cwd": "", "first": "", "t0": 0}
    read = 0
    with open(path, "rb") as f:
        for raw in f:
            read += len(raw)
            if not info["t0"]:
                m = _TS.search(raw)
                if m:
                    info["t0"] = calendar.timegm(time.strptime(m.group(1).decode(), "%Y-%m-%dT%H:%M:%S"))
            if not info["cwd"]:
                m = _CWD.search(raw)
                if m:
                    try:
                        info["cwd"] = json.loads(b'"' + m.group(1) + b'"')
                    except ValueError:
                        pass
            if not info["first"] and b'"type":"user"' in raw[:200]:
                try:
                    info["first"] = _first_text(json.loads(raw))
                except ValueError:
                    pass
            if (info["cwd"] and info["first"]) or read >= HEAD_MAX or (read >= HEAD and info["cwd"] and info["t0"]):
                break
    return info


def _last_title(m, needle: bytes, key: str, start: int, end: int) -> str:
    """[start, end) 里最后一条标题记录。标题会被反复写进存档（改一次写一条），最后一条才是现在的。"""
    i = m.rfind(needle, start, end)
    if i < 0:
        return ""
    a, b = m.rfind(b"\n", 0, i) + 1, m.find(b"\n", i)
    try:
        return str(json.loads(m[a: b if b >= 0 else end]).get(key) or "")
    except ValueError:
        return ""


def _cloud_id(b: str) -> str:
    """存档里记的是 cse_xxx，应用和接口里叫 session_xxx，后半截是同一个。"""
    b = str(b or "")
    return "session_" + b[4:] if b.startswith("cse_") else b


def _claude_one(path: str) -> dict | None:
    st = os.stat(path)
    c = _cache.get(path)
    if c and c["mtime"] == st.st_mtime and c["size"] == st.st_size:
        return c["out"]
    if not c or st.st_size < c["scanned"]:              # 第一次见，或者文件变小了（被重写过）：从头来
        c = {"scanned": 0, "enq": 0, "custom": "", "ai": "", "bridge": "", "human": 0, "cli": False, "head": None}
    if c["head"] is None or not (c["head"]["cwd"] and c["head"]["first"]):
        c["head"] = _head(path)
    if st.st_size > c["scanned"]:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            end = m.rfind(b"\n", c["scanned"], st.st_size) + 1     # 只算到最后一个完整的行
            if end > c["scanned"]:
                chunk = m[c["scanned"]:end]
                c["enq"] += chunk.count(N_ENQ)
                c["human"] += chunk.count(N_HUMAN)
                c["cli"] = c["cli"] or N_CLI in chunk
                del chunk
                c["custom"] = _last_title(m, N_TITLE_C, "customTitle", c["scanned"], end) or c["custom"]
                c["ai"] = _last_title(m, N_TITLE_A, "aiTitle", c["scanned"], end) or c["ai"]
                c["bridge"] = _last_title(m, N_BRIDGE, "bridgeSessionId", c["scanned"], end) or c["bridge"]
                c["scanned"] = end
    title = c["custom"] or c["ai"] or c["head"]["first"]
    out = None
    if title or c["head"]["cwd"]:                        # 空壳文件（没有任何对话内容）不列
        named = bool(c["custom"] or c["ai"])
        out = {"agent": "claude", "id": os.path.basename(path)[:-6], "title": " ".join(title.split())[:120],
               "cwd": c["head"]["cwd"], "mtime": st.st_mtime, "size": st.st_size, "turns": c["enq"], "named": named,
               "t0": c["head"]["t0"], "bridge": _cloud_id(c["bridge"]),
               # auto：存档里一条人发的消息都没有，也不是在终端界面里开的 → 程序调的（claude -p 之类）
               "auto": c["human"] == 0 and not c["cli"]}
    c.update(mtime=st.st_mtime, size=st.st_size, out=out)
    _cache[path] = c
    return out


def claude_sessions(home: str) -> list[dict]:
    out = []
    for path in glob.glob(os.path.join(home, ".claude", "projects", "*", "*.jsonl")):
        try:
            d = _claude_one(path)
        except OSError:
            continue
        if d:
            out.append(d)
    return out


def procs(home: str) -> list[dict]:
    """正在跑的 Claude 进程。Claude Code 自己在 ~/.claude/sessions/<pid>.json 里记着：哪个对话、应用那边的会话 id、忙不忙。
    server = 它是 Remote Control 后台服务的子进程（没有终端界面的那种）。"""
    out = []
    for f in glob.glob(os.path.join(home, ".claude", "sessions", "*.json")):
        try:
            d = json.loads(open(f).read())
            pid = int(d["pid"])
            stat = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            if d.get("procStart") and str(d["procStart"]) != stat[19]:
                continue                              # pid 被别的进程重新用了
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            continue                                  # 进程已经不在了的不算
        if d.get("sessionId"):
            out.append({"pid": pid, "ppid": int(stat[1]), "sid": str(d["sessionId"]), "cloud": str(d.get("bridgeSessionId") or ""),
                        "tmux": str(d.get("tmux") or "").split(":")[0], "status": str(d.get("status") or ""),
                        "server": b"--sdk-url" in cmd, "started": float(d.get("startedAt") or 0) / 1000})
    return out




if __name__ == "__main__":
    # 在别的机器上当脚本跑：python3 - <家目录>
    _home = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~")
    json.dump({"sessions": claude_sessions(_home), "procs": procs(_home), "now": time.time()}, sys.stdout)
