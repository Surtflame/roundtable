"""历史对话：把这台机器上 Claude Code 和 Codex 存的会话列出来，点一下就能在新窗口里接着聊。

只读它们自己的存档，不改：
  Claude Code  ~/.claude/projects/<目录名>/<会话 id>.jsonl      接着聊：claude --resume <id>
  Codex        ~/.codex/state_*.sqlite 里的 threads 表          接着聊：codex resume <id>

Claude 应用（Remote Control）里看到的标题和置顶不在本机的存档里，在 Anthropic 那边，这个程序自己取不到。留了一个口子：
  - <数据目录>/term/cloud_sessions.json（可选）：应用里的会话清单 {"sessions": [{"id", "title", "created_at", "updated_at", "pinned"}]}。
    有办法导出这份清单的话放在这里，列表就会显示应用里的标题和置顶；没有这个文件就只用本机存档里的标题。
  - 本机会话 ↔ 应用里的会话怎么对上：正在跑的 Claude 进程会在 ~/.claude/sessions/<pid>.json 里写明两边的 id，
    看到一对就记进库里（links）；在终端里带着 Remote Control 聊过的对话，存档里自己也记着（bridge-session 那种行）；
    没赶上的老会话，按“第一条消息的时间”和应用里的创建时间对（差一分钟以内才算）。

一个对话现在由谁带着（procs / holders）——这决定了点它的时候该怎么办（Claude Code 2.1.322 上实测过）：
  - 没有进程带着：claude --resume 就行。它以前开过 Remote Control 的话（存档里有 bridge-session），会自己接回应用里的同一个会话，两边同步。
  - 终端里的 Claude Code 带着（比如你自己在 tmux 里开的 claude --remote-control）：不要另起进程，直接接到那个 tmux 会话上（tmuxctl 的“借来的窗口”）。
    另起一个 claude --resume 的话，新进程会提示 “Remote Control not started here”，这时两个进程各聊各的；
    在新进程里敲 /remote-control 能把应用里的同一个会话挪过来（老进程收到 4090 退到一边，但进程还在）。
  - Remote Control 后台（claude remote-control 那个常驻服务）的子进程带着——在应用里新开的对话都是这种：它没有终端界面，
    没东西可接；存档里也不记应用那边的会话 id，所以在终端里 resume 接不回同一个应用会话。只能在应用里聊，或者明知会分叉另开一份。
"""
import calendar
import glob
import json
import os
import sqlite3
import time

from . import config, db

from . import scan


def claude_sessions() -> list[dict]:
    return scan.claude_sessions(config.HOME)


def procs() -> list[dict]:
    """正在跑的 Claude 进程（这台机器上的）。见 scan.procs。"""
    return scan.procs(config.HOME)


def codex_sessions() -> list[dict]:
    dbs = sorted(glob.glob(os.path.join(config.HOME, ".codex", "state_*.sqlite")),
                 key=lambda p: (len(p), p))          # 版本号大的在后面
    if not dbs:
        return []
    out = []
    try:
        con = sqlite3.connect(f"file:{dbs[-1]}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(threads)")}
            if not {"id", "cwd"} <= cols:
                return []
            pick = lambda *names: next((n for n in names if n in cols), None)  # noqa: E731
            t_ms, t_s = pick("updated_at_ms", "recency_at_ms"), pick("updated_at", "created_at")
            where = "WHERE archived=0" if "archived" in cols else ""
            for r in con.execute(f"SELECT * FROM threads {where} ORDER BY {t_ms or t_s or 'id'} DESC LIMIT 500"):
                d = dict(r)
                title = d.get("name") or d.get("title") or d.get("first_user_message") or d.get("preview") or ""
                mtime = (d[t_ms] / 1000) if t_ms and d.get(t_ms) else float(d.get(t_s) or 0) if t_s else 0
                src = str(d.get("source") or "")
                out.append({"agent": "codex", "id": str(d["id"]), "title": " ".join(str(title).split())[:120],
                            "cwd": str(d.get("cwd") or ""), "mtime": mtime, "size": 0,
                            "turns": 0, "named": bool(d.get("name") or d.get("title")), "auto": src == "exec"})
        finally:
            con.close()
    except sqlite3.Error:
        return out
    return out


# ---------- Claude 应用那边的标题和置顶 ----------

CLOUD_FILE = config.DATA / "cloud_sessions.json"
_cloud: dict = {"mtime": None, "by_id": {}}


def cloud() -> dict:
    """应用里的会话：id → {title, pinned, created}。文件没有就是空的。"""
    try:
        mt = CLOUD_FILE.stat().st_mtime
    except OSError:
        return {}
    if _cloud["mtime"] != mt:
        by = {}
        try:
            for c in json.loads(CLOUD_FILE.read_text()).get("sessions") or []:
                t = 0
                try:
                    t = calendar.timegm(time.strptime(str(c.get("created_at"))[:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError:
                    pass
                u = 0
                try:
                    u = calendar.timegm(time.strptime(str(c.get("updated_at"))[:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError:
                    pass
                by[str(c["id"])] = {"title": " ".join(str(c.get("title") or "").split())[:120], "pinned": bool(c.get("pinned")),
                                    "created": t, "updated": u}
        except (ValueError, KeyError, TypeError):
            by = {}
        _cloud.update(mtime=mt, by_id=by, matched=False)
    return _cloud["by_id"]


def holders(ps: list[dict]) -> dict:
    """本机会话 id → 现在带着它的那个进程。同一个对话被开了两份时：连着应用（Remote Control）的那个算正主，都连着或都没连就算后开的。"""
    by: dict = {}
    for p in sorted(ps, key=lambda p: p["started"]):
        cur = by.get(p["sid"])
        if cur is None or p["cloud"] or not cur["cloud"]:
            by[p["sid"]] = p
    return by


def _links(items: list[dict], lv: dict) -> dict:
    """本机会话 id → 应用里的会话 id。看到新的对应关系就记下来。"""
    links = db.get("links", {})
    before = dict(links)
    for s in items:
        if s.get("bridge"):
            links[s["id"]] = s["bridge"]              # 存档里自己记着的，最准
    for lid, v in lv.items():
        if v["cloud"]:
            links[lid] = v["cloud"]
    cl = cloud()
    if cl and not _cloud.get("matched"):
        # 清单刚导进来（或刚启动）：给没对上的老会话按开始时间配一次。一个应用里的会话只配给一个本机会话
        taken = set(links.values())
        for cid, c in cl.items():
            if cid in taken or not c["created"]:
                continue
            near = sorted((abs(s["t0"] - c["created"]), s["id"]) for s in items
                          if s["agent"] == "claude" and s.get("t0") and s["id"] not in links and abs(s["t0"] - c["created"]) <= 60)
            if near:
                links[near[0][1]] = cid
        # 还没对上的：本机存档里自己带着标题、而且和应用里某个会话的标题一字不差的，也算一对
        # （在终端里 resume 过的会话就是这种：进程换了，不再报应用那边的 id）。重名时挑最后活动时间最接近的
        taken = set(links.values())
        for s in items:
            if s["agent"] != "claude" or s["id"] in links or not s.get("named") or not s["title"]:
                continue
            same = sorted((abs(c["updated"] - s["mtime"]), cid) for cid, c in cl.items()
                          if cid not in taken and c["title"] == s["title"])
            if same:
                links[s["id"]] = same[0][1]
                taken.add(same[0][1])
        _cloud["matched"] = True
    if links != before:
        db.put("links", links)
    return links


def set_pin(agent: str, sid: str, pinned: bool) -> None:
    """自己手动置顶 / 取消置顶。记下的是“明确的选择”，会盖过应用里的置顶状态。"""
    pins = db.get("pins", {})
    pins[f"{agent}:{sid}"] = bool(pinned)
    db.put("pins", pins)


def set_hidden(agent: str, sid: str, hidden: bool) -> None:
    """隐藏 / 取消隐藏。同样记的是明确的选择：程序开的对话默认就在“已隐藏”里，手动取消隐藏之后就一直显示。"""
    hid = db.get("hidden", {})
    hid[f"{agent}:{sid}"] = bool(hidden)
    db.put("hidden", hid)


def enrich(items: list[dict], ps: list[dict] | None = None) -> list[dict]:
    """给每条会话补上：应用里的标题、是不是置顶、是不是隐藏、现在由哪个进程带着。
    items 里可以混着别的机器上的会话（带 env）；ps 是和它们同一批机器上的进程。"""
    lv = holders(procs() if ps is None else ps)
    links = _links(items, lv)
    cl = cloud()
    pins, hid = db.get("pins", {}), db.get("hidden", {})
    now = time.time()
    out = []
    for s in items:
        c = cl.get(links.get(s["id"], "")) if s["agent"] == "claude" else None
        l = lv.get(s["id"]) if s["agent"] == "claude" else None
        key = f"{s['agent']}:{s['id']}"
        pinned = pins[key] if key in pins else bool(c and c["pinned"])
        rc = bool(links.get(s["id"])) if s["agent"] == "claude" else False
        auto = bool(s.get("auto")) and not rc         # 连着应用里某个会话的，肯定是人开的
        out.append({**s, "title": (c["title"] if c and c["title"] else s["title"]), "pinned": pinned, "rc": rc, "auto": auto,
                    # hidden：手动选过的听手动的；没选过的，程序开的默认隐藏（置顶了的除外）
                    "hidden": hid[key] if key in hid else (auto and not pinned),
                    # running：有进程带着它。pid 是那个进程；working：它正在干活（不是停在那儿等人说话）
                    "running": bool(l), "pid": l["pid"] if l else 0, "working": bool(l and l["status"] == "busy"),
                    # synced：存档里记着应用那边的会话，在终端里 resume 会自己接回去，两边同步
                    "synced": bool(s.get("bridge")),
                    "busy": bool(l) or now - s["mtime"] < 120})
    return out


def sessions(q: str = "", hidden: bool = False, limit: int = 300, ps: list[dict] | None = None,
             extra: list[dict] | None = None) -> tuple[list[dict], int]:
    """各家合在一起：置顶的在前，其余新的在前。hidden=False 列没隐藏的，True 列“已隐藏”里的。
    extra 是别的机器上的会话。→ (这一页的会话, 已隐藏的总数)"""
    items = enrich(claude_sessions() + codex_sessions() + (extra or []), ps)
    n_hidden = sum(1 for s in items if s["hidden"])
    items = [s for s in items if s["hidden"] == hidden]
    words = q.lower().split()
    if words:
        items = [s for s in items if all(w in (s["title"] + " " + s["cwd"] + " " + s["id"]).lower() for w in words)]
    items.sort(key=lambda s: (not s["pinned"], -s["mtime"]))
    return items[:limit], n_hidden
