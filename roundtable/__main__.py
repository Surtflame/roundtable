"""命令行入口。

python -m roundtable                启动服务
python -m roundtable set-password   设置/修改登录密码（password 登录方式用）
python -m roundtable logout-all     让所有已登录的浏览器下线
python -m roundtable unlock         解除因为输错密码造成的登录锁定
python -m roundtable check          只检查配置和后端，不启动
python -m roundtable term           启动网页终端（可选的另一个进程，见 docs/terminal.md；python -m roundtable term help 看它的子命令）
"""
import asyncio
import getpass
import sys


def main() -> int:
    try:
        from . import config
        warnings = config.check()
    except Exception as e:
        if type(e).__name__ != "ConfigError":
            raise
        print(f"配置有问题：{e}", file=sys.stderr)
        return 2
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"

    if cmd == "term":
        from .term import cli
        return cli.main(sys.argv[2:])

    if cmd == "serve":
        import uvicorn
        uvicorn.run("roundtable.app:app", host=config.HOST, port=config.PORT, log_level="warning", access_log=False,
                    timeout_graceful_shutdown=1)
        return 0

    from . import auth, db
    db.con()
    if cmd == "set-password":
        if config.AUTH_MODE != "password":
            print(f"提醒：现在 ROUNDTABLE_AUTH={config.AUTH_MODE}，密码要等改成 password 之后才用得上。")
        if sys.stdin.isatty():
            pw = getpass.getpass(f"新密码（至少 {config.PASSWORD_MIN_LEN} 位）：")
            if pw != getpass.getpass("再输一遍："):
                print("两次输入不一致，没有修改。", file=sys.stderr)
                return 1
        else:
            pw = sys.stdin.readline().rstrip("\r\n")       # 方便脚本：echo "$PW" | python -m roundtable set-password
        try:
            auth.set_password(pw)
        except ValueError as e:
            print(f"没有修改：{e}", file=sys.stderr)
            return 1
        print("密码已设置，之前登录的浏览器全部下线了。服务不用重启。")
        return 0
    if cmd == "logout-all":
        print(f"已让 {auth.destroy_all_sessions()} 个登录失效。")
        return 0
    if cmd == "unlock":
        auth.unlock()
        print("登录锁定已解除。")
        return 0
    if cmd == "check":
        from . import backends
        for w in warnings:
            print("提醒：" + w)
        asyncio.run(backends.probe())
        st = backends.status()
        print(f"对外地址 {config.PUBLIC_URL}，监听 {config.HOST}:{config.PORT}，登录方式 {config.AUTH_MODE}，数据目录 {config.DATA}")
        if config.AUTH_MODE == "password" and not auth.has_password():
            print("提醒：还没有设密码。运行 python -m roundtable set-password")
        if config.AUTH_MODE == "google":
            print(f"Google 登录回调地址（填到 Google Cloud 控制台里）：{auth.redirect_uri()}")
        for name, title in (("claude", "Claude Code"), ("codex", "Codex"), ("codebuddy", "CodeBuddy"),
                            ("openrouter", "OpenRouter")):
            s = st[name]
            line = ("可用 " + s.get("version", "")).strip() if s["ok"] else "未启用 —— " + s["why"]
            if name == "codex" and s["ok"]:
                line += f"；沙箱：{'bubblewrap' if s.get('sandbox') == 'bwrap' else '没有'}"
                line += "" if s.get("logged_in") else "；看起来还没登录（运行 codex login）"
                line += f"；{s['note']}" if s.get("note") else ""
            if name == "codebuddy" and s["ok"]:
                line += f"；能选 {s['models']} 个模型"
                line += "；看起来还没登录（运行 codebuddy，按提示登录）" if s.get("logged_in") is False else ""
            print(f"  {title}: {line}")
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


sys.exit(main())
