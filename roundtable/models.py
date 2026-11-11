"""模型目录。key 形如 claude:opus / codex:<模型名> / codebuddy:<模型 id> / or:<OpenRouter 模型 id>。没启用的后端清单是空的。"""
import json
import time
from pathlib import Path

import httpx

from . import backends, config, db

# 用的是 Claude Code 的模型别名，具体指向哪个版本由你装的 CLI 决定；版本号是从真实回答里学来的（见 note_claude）。
# 要换清单（比如钉死某个完整的模型 id），设 ROUNDTABLE_CLAUDE_MODELS，见 .env.example。
_CLAUDE_DEFAULT = [
    {"id": "fable", "name": "Claude Fable", "desc": "最强，适合最难的问题"},
    {"id": "opus", "name": "Claude Opus", "desc": "复杂任务主力"},
    {"id": "sonnet", "name": "Claude Sonnet", "desc": "均衡、快"},
    {"id": "haiku", "name": "Claude Haiku", "desc": "最快，简单问题"},
]


def _claude_models() -> list[dict]:
    """named=True：显示名是用户自己在 ROUNDTABLE_CLAUDE_MODELS 里写的，原样用，不替他加版本号。"""
    out = []
    for item in config.CLAUDE_MODELS.split(","):
        mid, _, name = item.strip().partition("=")
        mid, name = mid.strip(), name.strip()
        if mid and all(ch.isalnum() or ch in "-._[]" for ch in mid):
            d = next((m for m in _CLAUDE_DEFAULT if m["id"] == mid), None)
            out.append({"id": mid, "name": name or (d["name"] if d else f"Claude {mid}"),
                        "desc": d["desc"] if d else "", "named": bool(name)})
    return out or [{**m, "named": False} for m in _CLAUDE_DEFAULT]


CLAUDE_MODELS = _claude_models()
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

_or_cache: dict = {"at": 0.0, "models": []}
_OR_CACHE_FILE = config.DATA / "openrouter_models.json"


# ---------- Claude 的别名实际指向哪个版本 ----------
# 圆桌给 Claude Code 的是别名（opus），它每次运行都会报出解析成了哪个模型 id（claude-opus-4-5-…）。
# 记下最近一次看到的，显示名就能带上版本号；Claude Code 升级后别名换了指向，下一次回答之后这里跟着变。

_claude_actual: dict | None = None


def claude_actual() -> dict:
    """别名 → 最近一次回答时它解析成的模型 id。存在设置表里（网页改不到这一项）。"""
    global _claude_actual
    if _claude_actual is None:
        v = db.get_setting("claude_actual", {})
        _claude_actual = {k: a for k, a in v.items() if isinstance(a, str)} if isinstance(v, dict) else {}
    return _claude_actual


def note_claude(asked: str, actual: str) -> bool:
    """记一笔。指向变了返回 True。"""
    if not asked or not isinstance(actual, str) or not actual.startswith("claude-"):
        return False
    actual = actual.split("[")[0]                   # claude-…[1m] 这种后缀不算版本的一部分
    cur = claude_actual()
    if asked == actual or cur.get(asked) == actual:
        return False
    cur[asked] = actual
    db.set_setting("claude_actual", cur)
    return True


def _claude_version(actual: str) -> str:
    """claude-opus-4-5-20251101 → "4.5"；claude-sonnet-4-20250514 → "4"；看不出来是空串。"""
    return ".".join([t for t in actual.split("[")[0].split("-") if t.isdigit() and len(t) <= 2][:2])


def _claude_pretty(mid: str) -> str:
    """完整的模型 id → 像样的名字：claude-opus-4-5-20251101 → "Claude Opus 4.5"。认不出来是空串。"""
    toks = mid.split("[")[0].split("-")
    fam = next((t for t in toks[1:] if t.isalpha()), "")
    if toks[0] != "claude" or not fam:
        return ""
    return " ".join(x for x in ("Claude", fam.capitalize(), _claude_version(mid), mid[len(mid.split("[")[0]):]) if x)


def claude_name(mid: str, actual: str | None = None) -> str:
    """显示名。actual 是某个回答实际用的模型 id；None = 按别名现在指向的版本；"" = 不知道，就不带版本号。"""
    m = next((m for m in CLAUDE_MODELS if m["id"] == mid), None)
    if m and m["named"]:
        return m["name"]
    if mid.startswith("claude-"):                   # 清单里写的就是完整的模型 id
        return _claude_pretty(mid) or (m["name"] if m else mid)
    base = m["name"] if m else next((d["name"] for d in _CLAUDE_DEFAULT if d["id"] == mid), f"Claude {mid}")
    if actual is None:
        actual = claude_actual().get(mid, "")
    if not actual:
        return base
    ver = _claude_version(actual) if mid in actual.split("[")[0].split("-") else ""
    return f"{base} {ver}" if ver else f"{base}（{actual}）"


def claude_catalog() -> list[dict]:
    if not backends.CAPS["claude"]["ok"]:
        return []
    efforts = CLAUDE_EFFORTS if "--effort" in backends.CAPS["claude"]["flags"] else []
    out = []
    for m in CLAUDE_MODELS:
        actual = "" if m["id"].startswith("claude-") else claude_actual().get(m["id"], "")
        desc = m["desc"]
        if actual and m["named"]:                   # 名字是用户自己起的：不改它，把实际指向写在说明里
            desc = " · ".join(x for x in (desc, f"当前指向 {actual}") if x)
        out.append({"key": f"claude:{m['id']}", "provider": "claude", "id": m["id"], "name": claude_name(m["id"]),
                    "desc": desc, "actual": actual, "efforts": efforts, "default_effort": "", "vision": True,
                    "image_out": False, "web": True})
    return out


def _codex_entries(models) -> list[dict]:
    out = []
    for m in models if isinstance(models, list) else []:
        if not isinstance(m, dict) or not m.get("slug") or m.get("visibility") != "list":
            continue
        efforts = [e.get("effort") for e in m.get("supported_reasoning_levels") or [] if e.get("effort")]
        out.append({"key": f"codex:{m['slug']}", "provider": "codex", "id": m["slug"],
                    "name": m.get("display_name") or m["slug"], "desc": m.get("description", ""),
                    "efforts": efforts, "default_effort": m.get("default_reasoning_level") or "",
                    "vision": "image" in (m.get("input_modalities") or []), "image_out": True, "web": True,
                    "ctx": m.get("context_window")})
    return out


def codex_catalog() -> list[dict]:
    if not backends.CAPS["codex"]["ok"]:
        return []
    out = []
    try:
        out = _codex_entries(json.loads(Path(config.CODEX_HOME, "models_cache.json").read_text()).get("models"))
    except Exception:
        pass
    if not out:         # codex 还没拉过模型清单（第一次用之前就是这样）：先用这个版本自带的那份
        try:
            out = _codex_entries(backends.CAPS["codex"].get("builtin_models"))
        except Exception:
            pass
    if not out:         # 连自带的也拿不到（旧版本）：给一个"用它自己默认模型"的入口
        out = [{"key": "codex:default", "provider": "codex", "id": "default", "name": "Codex（默认模型）",
                "desc": "用 codex 自己配置的模型", "efforts": [], "default_effort": "",
                "vision": True, "image_out": True, "web": True}]
    return out


def _cb_credits(mult: str) -> str:
    """额度倍率（"0.79"）→ 界面上的一句话。同样多的字，倍率高的模型扣的额度多。"""
    try:
        return "不扣额度" if float(mult) == 0 else f"额度 ×{float(mult):g}"
    except ValueError:
        return ""


def codebuddy_catalog() -> list[dict]:
    """CodeBuddy 现在能选的模型，全列出来。清单是问 CLI 问来的（backends.codebuddy_refresh），跟着它登录的账号走。"""
    cap = backends.CAPS["codebuddy"]
    if not cap["ok"]:
        return []
    out, seen = [], set()
    for x in cap["models"]:
        name = str(x.get("name") or x["id"])
        if name in seen:                            # 有重名的（Hy3 就有两个），后一个带上 id
            name = f"{name}（{x['id']}）"
        seen.add(str(x.get("name") or x["id"]))
        r = x.get("reasoning") if isinstance(x.get("reasoning"), dict) else {}
        efforts = [e for e in r.get("supportedEfforts") or [] if isinstance(e, str)]
        credits = str(x.get("credits") or "").replace("credits", "").strip().lstrip("x×")
        out.append({"key": f"codebuddy:{x['id']}", "provider": "codebuddy", "id": x["id"], "name": name,
                    "desc": " · ".join(t for t in (str(x.get("descriptionZh") or ""),
                                                  _cb_credits(credits)) if t),
                    # 没声明档位的模型只有一个固定的思考强度，就不给选了
                    "efforts": efforts if "--effort" in cap["flags"] else [],
                    "default_effort": str(r.get("defaultEffort") or r.get("effort") or ""),
                    # 不能直接收图的模型，CLI 会让它用看图工具转述，所以都算能看图
                    "vision": True, "image_out": False, "web": True, "ctx": x.get("maxInputTokens")})
    return out


def _or_entry(m: dict) -> dict:
    arch = m.get("architecture") or {}
    pr = m.get("pricing") or {}

    def per_m(v):
        try:
            return round(float(v) * 1e6, 4)
        except Exception:
            return None
    sp = m.get("supported_parameters") or []
    efforts = ((m.get("reasoning") or {}).get("supported_efforts")) or (
        ["low", "medium", "high"] if "reasoning" in sp else [])
    return {"key": f"or:{m['id']}", "provider": "or", "id": m["id"], "name": m.get("name") or m["id"],
            "desc": "", "efforts": efforts, "default_effort": "",
            "vision": "image" in (arch.get("input_modalities") or []),
            "file_in": "file" in (arch.get("input_modalities") or []),
            "image_out": "image" in (arch.get("output_modalities") or []),
            "tools": "tools" in sp,
            "web": True, "ctx": m.get("context_length"),
            "price_in": per_m(pr.get("prompt")), "price_out": per_m(pr.get("completion")),
            "created": m.get("created")}


async def openrouter_catalog(force: bool = False) -> list[dict]:
    if not config.openrouter_key():
        return []
    if _or_cache["models"] and not force and time.time() - _or_cache["at"] < 6 * 3600:
        return _or_cache["models"]
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get("https://openrouter.ai/api/v1/models")
            r.raise_for_status()
            data = r.json().get("data", [])
        models = [_or_entry(m) for m in data if m.get("id") and not m["id"].endswith(":batch")]
        models.sort(key=lambda m: -(m.get("created") or 0))
        _or_cache.update(at=time.time(), models=models)
        try:
            _OR_CACHE_FILE.write_text(json.dumps(models, ensure_ascii=False))
        except OSError:
            pass
    except Exception:
        if not _or_cache["models"]:
            try:
                _or_cache.update(at=time.time() - 5 * 3600, models=json.loads(_OR_CACHE_FILE.read_text()))
            except Exception:
                pass
    return _or_cache["models"]


async def catalog() -> dict:
    await backends.codebuddy_ready()
    if backends.CAPS["codebuddy"]["account"]:
        backends.codebuddy_refresh_soon()
    else:
        await backends.codebuddy_refresh()          # 还没登录（或还没问出来）：等它，刚登录完刷新页面就能看到
    return {"claude": claude_catalog(), "codex": codex_catalog(), "codebuddy": codebuddy_catalog(),
            "openrouter": await openrouter_catalog(), "favorites": db.get_setting("or_favorites", []), "backends": backends.status()}


async def lookup(key: str) -> dict | None:
    prov, _, mid = key.partition(":")
    if prov == "claude":
        return next((m for m in claude_catalog() if m["id"] == mid), None)
    if prov == "codex":
        return next((m for m in codex_catalog() if m["id"] == mid), None)
    if prov == "codebuddy":
        return next((m for m in codebuddy_catalog() if m["id"] == mid), None)
    if prov == "or":
        return next((m for m in await openrouter_catalog() if m["id"] == mid), None)
    return None


def label_sync(key: str, actual: str | None = None) -> str:
    """不发网络请求的名字查找（导出、提示词里用）。actual 只对 Claude 有意义，见 claude_name。"""
    prov, _, mid = key.partition(":")
    if prov == "claude":
        return claude_name(mid, actual)
    if prov == "codex":
        return next((m["name"] for m in codex_catalog() if m["id"] == mid), mid)
    if prov == "codebuddy":
        return next((m["name"] for m in codebuddy_catalog() if m["id"] == mid), mid)
    if prov == "or":
        return next((m["name"] for m in _or_cache["models"] if m["id"] == mid), mid)
    return key
