"""规则模板：用户自己写的几份提示词，提问时勾上哪几份，就把哪几份带进系统提示。

和“自定义指令”的区别：自定义指令只有一份、每次都带；规则模板可以存很多份，按问题挑着带。

发消息那一刻把勾上的模板抄一份记在这一轮上（turns.config.rules），之后模板再改、再删，
都不影响已经问过的轮次：重新生成、追加互评、追加综合用的还是当时那一份。
"""
from . import db

MAX_TEMPLATES = 50
MAX_NAME = 60
MAX_BODY = 10_000          # 一份模板最多多少字
MAX_PER_TURN = 10
# 一次带进去的总量（UTF-8 字节）。系统提示是作为一个命令行参数交给 claude / codex 的，
# Linux 单个参数最长 128KB，这里给自定义指令和固定说明留出余量。
MAX_TURN_BYTES = 64_000


def _pub(r) -> dict:
    return {"id": r["id"], "name": r["name"], "body": r["body"], "updated_at": r["updated_at"]}


def list_all() -> list[dict]:
    return [_pub(r) for r in db.q("SELECT * FROM rule_templates ORDER BY created_at, id")]


def _clean(name, body, skip_id: str | None = None) -> tuple[str, str]:
    if not isinstance(name, str) or not isinstance(body, str):
        raise ValueError("名字和内容都要填")
    name = " ".join(name.split())
    body = body.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()
    if not name:
        raise ValueError("给模板起个名字")
    if len(name) > MAX_NAME:
        raise ValueError(f"名字最多 {MAX_NAME} 个字")
    if not body:
        raise ValueError("模板内容是空的")
    if len(body) > MAX_BODY:
        raise ValueError(f"内容最多 {MAX_BODY} 个字，现在是 {len(body)} 个")
    same = db.q1("SELECT id FROM rule_templates WHERE name=?", (name,))
    if same and same["id"] != skip_id:
        raise ValueError("已经有一份同名的模板，换个名字")
    return name, body


def create(name, body) -> dict:
    name, body = _clean(name, body)
    if db.q1("SELECT COUNT(*) AS n FROM rule_templates")["n"] >= MAX_TEMPLATES:
        raise ValueError(f"最多存 {MAX_TEMPLATES} 份模板，先删掉几份不用的")
    rid, now = db.new_id(), db.now()
    db.x("INSERT INTO rule_templates(id,name,body,created_at,updated_at) VALUES(?,?,?,?,?)", (rid, name, body, now, now))
    return _pub(db.q1("SELECT * FROM rule_templates WHERE id=?", (rid,)))


def update(rid: str, name, body) -> dict:
    """模板不存在时抛 KeyError。"""
    if not db.q1("SELECT 1 FROM rule_templates WHERE id=?", (rid,)):
        raise KeyError(rid)
    name, body = _clean(name, body, skip_id=rid)
    db.x("UPDATE rule_templates SET name=?, body=?, updated_at=? WHERE id=?", (name, body, db.now(), rid))
    return _pub(db.q1("SELECT * FROM rule_templates WHERE id=?", (rid,)))


def delete(rid: str) -> None:
    db.x("DELETE FROM rule_templates WHERE id=?", (rid,))


def snapshot(ids) -> list[dict]:
    """把这次勾上的模板 id 换成内容快照 [{id, name, body}]。已经删掉的 id 直接跳过。"""
    out, seen = [], set()
    for rid in ids if isinstance(ids, list) else []:
        if not isinstance(rid, str) or rid in seen:
            continue
        seen.add(rid)
        r = db.q1("SELECT id, name, body FROM rule_templates WHERE id=?", (rid,))
        if r:
            out.append({"id": r["id"], "name": r["name"], "body": r["body"]})
    if len(out) > MAX_PER_TURN:
        raise ValueError(f"一次最多带 {MAX_PER_TURN} 份规则模板")
    if sum(len(r["body"].encode()) + len(r["name"].encode()) for r in out) > MAX_TURN_BYTES:
        raise ValueError("勾上的规则模板加起来太长了（上限大约两万个汉字），去掉几份再发")
    return out


def of_turn(cfg: dict | None) -> list[dict]:
    """一轮的配置里记着的模板快照（老数据没有这一项）。"""
    rs = (cfg or {}).get("rules")
    return [r for r in rs if isinstance(r, dict) and r.get("body")] if isinstance(rs, list) else []


def prompt(rs: list[dict]) -> str:
    """拼进系统提示的那一段。"""
    if not rs:
        return ""
    out = ["\n用户为这次提问选用了下面的规则模板。它们是用户本人写的要求，回答、互评、综合时都要遵守。"]
    for r in rs:
        name = str(r.get("name") or "").replace('"', "'").replace("\n", " ")
        out.append(f'\n<rule_template name="{name}">\n{r["body"]}\n</rule_template>')
    return "\n".join(out)


def names_only(cfg: dict | None) -> dict | None:
    """给前端的配置：模板只留 id 和名字，正文不跟着每次推送来回跑。"""
    if not cfg or "rules" not in cfg:
        return cfg
    return {**cfg, "rules": [{"id": r.get("id"), "name": r.get("name")} for r in of_turn(cfg)]}
