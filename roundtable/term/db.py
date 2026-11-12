"""SQLite：解锁会话、动态验证码的状态、操作记录。终端本身的状态都在 tmux 里，这里不存。"""
import json
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS unlocks(
  token_hash TEXT PRIMARY KEY,       -- 只存 Cookie 值的哈希
  email TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_active REAL NOT NULL,
  ip TEXT NOT NULL DEFAULT '',
  ua TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,                -- unlock / unlock_fail / lock / enroll / ws / window / fs
  detail TEXT NOT NULL DEFAULT '',
  ip TEXT NOT NULL DEFAULT ''
);
"""

_con: sqlite3.Connection | None = None


def con() -> sqlite3.Connection:
    global _con
    if _con is None:
        config.DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
        _con = sqlite3.connect(config.DB_PATH, isolation_level=None, check_same_thread=False)
        _con.row_factory = sqlite3.Row
        _con.execute("PRAGMA journal_mode=WAL")
        _con.execute("PRAGMA synchronous=NORMAL")
        _con.executescript(SCHEMA)
    return _con


def q(sql: str, args=()) -> list[sqlite3.Row]:
    return con().execute(sql, args).fetchall()


def q1(sql: str, args=()) -> sqlite3.Row | None:
    return con().execute(sql, args).fetchone()


def x(sql: str, args=()) -> int:
    return con().execute(sql, args).rowcount


def get(key: str, default=None):
    r = q1("SELECT value FROM kv WHERE key=?", (key,))
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except ValueError:
        return default


def put(key: str, value) -> None:
    x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, json.dumps(value, ensure_ascii=False)))


def audit(kind: str, detail: str = "", ip: str = "") -> None:
    x("INSERT INTO audit(ts,kind,detail,ip) VALUES(?,?,?,?)", (time.time(), kind, detail[:500], ip[:80]))
    print(f"audit {kind} {ip} {detail[:200]}", flush=True)
