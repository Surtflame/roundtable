"""远程环境的清单：别的机器，窗口可以开在它们上面（见 remote.py）。

存在 data/envs.json 里，随时可以增删，不用重启：
  - 网页上「新窗口 → 添加远程环境」会开一个 Claude Code 对话带着用户配，配好后由它调 `python -m roundtable term env-add` 登记
  - 也可以自己在命令行跑 env-add / env-list / env-remove
每一项：{"label": 显示的名字, "short": 标签上的短名, "ssh": "用户@主机", "port": 端口, "key": 私钥路径, "cwd": 默认目录, "home": 那边的家目录}
私钥只写路径，文件本身在别处（一般是 ~/.ssh/keys/）；这个清单里没有密码。
"""
import json
import os
import re
import subprocess

from . import config

FILE = config.DATA / "envs.json"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
SSH_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}@[A-Za-z0-9.:_-]{1,253}$")
_cache: dict = {"mtime": None, "envs": {}}


class EnvError(Exception):
    pass


def all() -> dict:  # noqa: A001
    """→ {名字: 配置}。文件变了会自己重读。"""
    if config.ENVS_OVERRIDE is not None:              # 仅测试用
        return config.ENVS_OVERRIDE
    try:
        mt = FILE.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] != mt:
        try:
            d = json.loads(FILE.read_text())
            envs = {k: v for k, v in d.items() if NAME_RE.match(k) and isinstance(v, dict) and _valid(v) is None}
        except (OSError, ValueError):
            envs = {}
        _cache.update(mtime=mt, envs=envs)
    return _cache["envs"]


def get(name: str) -> dict | None:
    return all().get(name or "")


def public() -> list[dict]:
    """给浏览器的：只有名字和目录，连接方式不出服务器。"""
    return [{"key": k, "label": e["label"], "short": e.get("short") or e["label"], "cwd": e["cwd"], "home": e.get("home") or ""}
            for k, e in all().items()]


def _valid(e: dict) -> str | None:
    if not isinstance(e.get("label"), str) or not e["label"].strip() or len(e["label"]) > 40:
        return "要有显示用的名字（label），40 个字以内"
    if not isinstance(e.get("ssh"), str) or not SSH_RE.match(e["ssh"]) or e["ssh"].startswith("-"):
        return "ssh 要写成 用户@主机"
    if e.get("port") is not None and not (isinstance(e["port"], int) and 1 <= e["port"] <= 65535):
        return "端口不对"
    if not isinstance(e.get("key"), str) or not e["key"].startswith("/"):
        return "key 要写私钥文件的完整路径"
    for k in ("cwd", "home"):
        v = e.get(k)
        if not isinstance(v, str) or not v.startswith("/") or "\n" in v or "\x00" in v:
            return f"{k} 要写完整路径"
    return None


def _write(envs: dict) -> None:
    config.DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(envs, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)
    _cache["mtime"] = None


def probe(e: dict) -> dict:
    """用登记的方式真连一次：能不能免密登录、那边的家目录、有没有 tmux / python3 / claude / codex。连不上抛 EnvError。"""
    argv = ["ssh", "-T", "-i", e["key"], "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=15", *(["-p", str(e["port"])] if e.get("port") else []), e["ssh"],
            'echo "home=$HOME"; for c in tmux python3; do echo "$c=$(command -v $c)"; done; '
            'bash -lc \'for c in claude codex; do echo "$c=$(command -v $c)"; done\' 2>/dev/null; echo ok=1']
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=40, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise EnvError("连接超时") from None
    got = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    if r.returncode != 0 or got.get("ok") != "1":
        raise EnvError("用这把钥匙免密登录失败：" + (r.stderr.strip()[-300:] or f"退出码 {r.returncode}"))
    return got


def add(name: str, label: str, ssh: str, key: str, cwd: str = "", home: str = "", port: int | None = None, short: str = "",
        check: bool = True) -> tuple[dict, list[str]]:
    """登记（或覆盖）一个环境。→ (写进去的配置, 提醒)。check=True 会先真连一次。"""
    if not NAME_RE.match(name or ""):
        raise EnvError("名字只能用小写字母、数字、- 和 _，最长 24 个字符")
    key = os.path.abspath(os.path.expanduser(key))
    if not os.path.isfile(key):
        raise EnvError(f"私钥文件不存在：{key}")
    if os.stat(key).st_mode & 0o077:
        raise EnvError(f"私钥文件的权限太宽，先 chmod 600 {key}")
    e = {"label": label.strip(), "short": (short or label).strip()[:12], "ssh": ssh.strip(), "key": key,
         "cwd": cwd or home or "/", "home": home or "/"}
    if port and port != 22:
        e["port"] = int(port)
    notes = []
    if check:
        got = probe(e)
        e["home"] = home or got.get("home") or "/"
        e["cwd"] = cwd or e["home"]
        if not got.get("tmux"):
            raise EnvError("那台机器上没有 tmux，先装上（窗口是开在它的 tmux 里的）")
        if not got.get("python3"):
            notes.append("那台机器上没有 python3：窗口能开，但列不出那边的历史对话")
        if not got.get("claude"):
            notes.append("那台机器的登录 shell 里找不到 claude：只能开 Shell 窗口")
    bad = _valid(e)
    if bad:
        raise EnvError(bad)
    envs = dict(all())
    envs[name] = e
    _write(envs)
    return e, notes


def remove(name: str) -> bool:
    envs = dict(all())
    if name not in envs:
        return False
    del envs[name]
    _write(envs)
    return True
