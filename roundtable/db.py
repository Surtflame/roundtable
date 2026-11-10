"""SQLite（WAL）。单用户、单进程，所有读写都在事件循环线程里做。"""
import os
import json
import secrets
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  title_auto INTEGER NOT NULL DEFAULT 1,
  pinned INTEGER NOT NULL DEFAULT 0,
  config TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns(
  id TEXT PRIMARY KEY,
  conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  user_text TEXT NOT NULL,
  attachments TEXT NOT NULL DEFAULT '[]',
  mode TEXT NOT NULL,
  config TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_conv ON turns(conv_id, seq);
CREATE TABLE IF NOT EXISTS responses(
  id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
  conv_id TEXT NOT NULL,
  model TEXT NOT NULL,
  effort TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL,              -- answer | review | synth
  round INTEGER NOT NULL DEFAULT 0,
  pos INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,             -- pending | running | done | error | stopped | skipped
  text TEXT NOT NULL DEFAULT '',
  thinking TEXT NOT NULL DEFAULT '',
  activity TEXT NOT NULL DEFAULT '[]',
  images TEXT NOT NULL DEFAULT '[]',
  usage TEXT NOT NULL DEFAULT '{}',
  error TEXT,
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS responses_turn ON responses(turn_id, round, pos);
CREATE INDEX IF NOT EXISTS responses_conv ON responses(conv_id);
CREATE TABLE IF NOT EXISTS files(
  id TEXT PRIMARY KEY,
  conv_id TEXT,
  origin TEXT NOT NULL,             -- upload | generated
  kind TEXT NOT NULL,               -- image | pdf | text
  name TEXT NOT NULL,
  mime TEXT NOT NULL,
  size INTEGER NOT NULL,
  path TEXT NOT NULL,
  text_path TEXT,
  meta TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS files_conv ON files(conv_id);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- 登录相关。sessions.id 存的是 Cookie 值的 SHA-256，库泄露了也拿不到能用的 Cookie。
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY,
  user TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS oidc_pending(
  state TEXT PRIMARY KEY,           -- 同样只存哈希
  nonce TEXT NOT NULL,
  verifier TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""

_con: sqlite3.Connection | None = None


def con() -> sqlite3.Connection:
    global _con
    if _con is None:
        config.DATA.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(config.DATA, 0o700)          # 对话、上传的文件、密码哈希都在这里，别人不该能读
        except OSError:
            pass
        _con = sqlite3.connect(config.DB_PATH, isolation_level=None, check_same_thread=False)
        _con.row_factory = sqlite3.Row
        _con.execute("PRAGMA journal_mode=WAL")
        _con.execute("PRAGMA synchronous=NORMAL")
        _con.execute("PRAGMA foreign_keys=ON")
        _con.executescript(SCHEMA)
    return _con


def new_id() -> str:
    return secrets.token_hex(8)


def now() -> float:
    return time.time()


def q(sql: str, args=()) -> list[sqlite3.Row]:
    return con().execute(sql, args).fetchall()


def q1(sql: str, args=()) -> sqlite3.Row | None:
    return con().execute(sql, args).fetchone()


def x(sql: str, args=()) -> None:
    con().execute(sql, args)


def get_setting(key: str, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except Exception:
        return default


def set_setting(key: str, value) -> None:
    x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, json.dumps(value, ensure_ascii=False)))


JSON_COLS = {"config", "attachments", "activity", "images", "usage", "meta"}


def row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        try:
            d[k] = json.loads(d[k]) if d[k] else None
        except Exception:
            d[k] = None
    return d
