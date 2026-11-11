"""模型目录。key 形如 claude:opus / codex:<模型名> / or:<OpenRouter 模型 id>。没启用的后端清单是空的。"""
import json
import time
from pathlib import Path

import httpx

from . import backends, config, db

# 用的是 Claude Code 的模型别名，具体指向哪个版本由你装的 CLI 决定。
# 要换清单（比如钉死某个完整的模型 id），设 ROUNDTABLE_CLAUDE_MODELS，见 .env.example。
_CLAUDE_DEFAULT = [
    {"id": "fable", "name": "Claude Fable", "desc": "最强，适合最难的问题"},
    {"id": "opus", "name": "Claude Opus", "desc": "复杂任务主力"},
    {"id": "sonnet", "name": "Claude Sonnet", "desc": "均衡、快"},
    {"id": "haiku", "name": "Claude Haiku", "desc": "最快，简单问题"},
]


def _claude_models() -> list[dict]:
    out = []
    for item in config.CLAUDE_MODELS.split(","):
        mid, _, name = item.strip().partition("=")
        mid = mid.strip()
        if mid and all(ch.isalnum() or ch in "-._[]" for ch in mid):
            out.append({"id": mid, "name": name.strip() or f"Claude {mid}", "desc": ""})
    return out or _CLAUDE_DEFAULT


CLAUDE_MODELS = _claude_models()
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

_or_cache: dict = {"at": 0.0, "models": []}
_OR_CACHE_FILE = config.DATA / "openrouter_models.json"


def claude_catalog() -> list[dict]:
    if not backends.CAPS["claude"]["ok"]:
        return []
    efforts = CLAUDE_EFFORTS if "--effort" in backends.CAPS["claude"]["flags"] else []
    return [{"key": f"claude:{m['id']}", "provider": "claude", "id": m["id"], "name": m["name"],
             "desc": m["desc"], "efforts": efforts, "default_effort": "", "vision": True,
             "image_out": False, "web": True} for m in CLAUDE_MODELS]


def codex_catalog() -> list[dict]:
    if not backends.CAPS["codex"]["ok"]:
        return []
    out = []
    try:
        d = json.loads(Path(config.CODEX_HOME, "models_cache.json").read_text())
        for m in d.get("models", []):
            if m.get("visibility") != "list":
                continue
            efforts = [e.get("effort") for e in m.get("supported_reasoning_levels", []) if e.get("effort")]
            out.append({"key": f"codex:{m['slug']}", "provider": "codex", "id": m["slug"],
                        "name": m.get("display_name") or m["slug"], "desc": m.get("description", ""),
                        "efforts": efforts, "default_effort": m.get("default_reasoning_level") or "",
                        "vision": "image" in (m.get("input_modalities") or []), "image_out": True, "web": True,
                        "ctx": m.get("context_window")})
    except Exception:
        pass
    if not out:         # codex 还没拉过模型清单（第一次用之前就是这样）：给一个"用它自己默认模型"的入口
        out = [{"key": "codex:default", "provider": "codex", "id": "default", "name": "Codex（默认模型）",
                "desc": "用 codex 自己配置的模型", "efforts": [], "default_effort": "",
                "vision": True, "image_out": True, "web": True}]
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
    return {"claude": claude_catalog(), "codex": codex_catalog(), "openrouter": await openrouter_catalog(),
            "favorites": db.get_setting("or_favorites", []), "backends": backends.status()}


async def lookup(key: str) -> dict | None:
    prov, _, mid = key.partition(":")
    if prov == "claude":
        return next((m for m in claude_catalog() if m["id"] == mid), None)
    if prov == "codex":
        return next((m for m in codex_catalog() if m["id"] == mid), None)
    if prov == "or":
        return next((m for m in await openrouter_catalog() if m["id"] == mid), None)
    return None


def label_sync(key: str) -> str:
    """不发网络请求的名字查找（导出、提示词里用）。"""
    prov, _, mid = key.partition(":")
    if prov == "claude":
        return next((m["name"] for m in CLAUDE_MODELS if m["id"] == mid), f"Claude {mid}")
    if prov == "codex":
        return next((m["name"] for m in codex_catalog() if m["id"] == mid), mid)
    if prov == "or":
        return next((m["name"] for m in _or_cache["models"] if m["id"] == mid), mid)
    return key
