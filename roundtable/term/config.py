"""网页终端的配置。对外地址、登录方式、数据目录跟着圆桌本身（../config.py），这里是终端自己的几项。

这个站等于服务器的 shell。访问要过两道：
  1. 圆桌的登录（见 auth.py：从对话页面递一张一次性票据过来，换成终端自己的会话 Cookie；不登录的模式只接受本机直连）
  2. 动态验证码（TOTP，谷歌验证器那种）；不登录的模式默认不开这一道
ROUNDTABLE_TERM_TEST_* 只给本机测试用。
"""
import os
import tempfile
from pathlib import Path

from .. import config as rt

ROOT = rt.ROOT
STATIC_DIR = ROOT / "static_term"
DATA = rt.DATA / "term"
DB_PATH = DATA / "term.db"

HOST = rt.TERM_HOST
PORT = rt.TERM_PORT
ORIGIN = rt.TERM_ORIGIN
HTTPS = ORIGIN.startswith("https://")
# 左边栏顶上的“对话 | 终端”切换：对话那一边是圆桌。两个站看起来是一个东西，但故意放在两个来源下——
# 圆桌的页面要显示模型和网页上来的内容，万一那边出了注入的问题，隔着来源碰不到这边的 shell。
CHAT_URL = rt.PUBLIC_URL + "/"
CHAT_ORIGIN = rt.ORIGIN

# --- 动态验证码（TOTP） ---
_totp = rt._get("ROUNDTABLE_TERM_TOTP").lower()
if _totp not in ("", "on", "off"):
    raise rt.ConfigError(f"ROUNDTABLE_TERM_TOTP 只能是 on / off，现在是 {_totp!r}")
# 默认：要登录的部署一定开；只在本机用（ROUNDTABLE_AUTH=none）的默认不开——能连到回环地址的人本来就有 shell
TOTP = (_totp == "on") if _totp else rt.AUTH_MODE != "none"
UNLOCK_TTL = rt._int("ROUNDTABLE_TERM_UNLOCK_HOURS", 12, 1, 720) * 3600      # 输一次验证码最多管多久
UNLOCK_IDLE = rt._int("ROUNDTABLE_TERM_IDLE_MIN", 120, 5, 43200) * 60        # 这么久没有操作就锁上
TOTP_MAX_FAILS = 5                # 连错这么多次……
TOTP_LOCKOUT = 5 * 60             # ……锁这么久（秒）
TOTP_ISSUER = "Roundtable 终端"
HANDOFF_TTL = 60                  # 从对话页面递过来的票据多久作废（秒）
WS_TICKET_TTL = 30                # 连终端前换的一次性票据多久作废（秒）
REVALIDATE_EVERY = int(os.environ.get("ROUNDTABLE_TERM_TEST_REVALIDATE") or 30)   # 已经连上的终端，隔多久重新查一次登录（秒）

# --- tmux ---
# 用单独的套接字，和这台机器上别的 tmux 完全分开。从 SSH 里也能接进来：tmux -L roundtable attach -t <窗口名>
TMUX_SOCKET = os.environ.get("ROUNDTABLE_TERM_TEST_SOCKET") or "roundtable"
TMUX_CONF = Path(__file__).resolve().parent / "tmux.conf"
# tmux 服务没起来就自己起一个。用 systemd 的话建议另外装 deploy/roundtable-tmux.service：
# 那样重启终端的网页服务不会带走窗口（自己起的 tmux 和网页服务在同一个控制组里，会被一起停掉）
TMUX_AUTOSTART = True
TMUX_TMPDIR = os.environ.get("ROUNDTABLE_TERM_TEST_TMUX_TMPDIR") or ""
MAX_WINDOWS = 40

HOME = rt.HOME
DEFAULT_CWD = str(Path(rt._get("ROUNDTABLE_TERM_CWD") or HOME).expanduser())

# --- 远程环境（别的机器）：清单在 DATA/envs.json，见 envs.py ---
ENVS_OVERRIDE = None
# SSH 连接复用的套接字放哪。这种套接字的路径有长度上限（一百来个字符），数据目录太深就放到临时目录下
SSH_CTL_DIR = os.environ.get("ROUNDTABLE_TERM_TEST_SSH_CTL") or (
    str(DATA) if len(str(DATA)) < 48 else os.path.join(tempfile.gettempdir(), f"roundtable-term-{os.getuid()}"))

# --- 文件管理 ---
MAX_EDIT = 2 * 1024 * 1024        # 网页里能打开编辑的文本文件上限
MAX_UPLOAD = rt._int("ROUNDTABLE_TERM_MAX_UPLOAD_MB", 2048, 1, 102400) * 1024 * 1024    # 单个上传上限
MAX_LIST = 5000                   # 一个目录最多列多少项
# 这些目录下面，文件管理不许改、不许删、不许往里传（终端里照样能动，这里只是防手滑）。逗号分隔的完整路径
FS_PROTECTED = tuple(p.rstrip("/") for p in (x.strip() for x in rt._get("ROUNDTABLE_TERM_PROTECT").split(",")) if p.startswith("/"))
