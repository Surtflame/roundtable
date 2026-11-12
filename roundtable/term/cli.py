"""python -m roundtable term                 启动终端的网页服务（另一个进程；先在 .env 里设 ROUNDTABLE_TERM_URL）
python -m roundtable term status          看一眼现在的状态
python -m roundtable term totp-reset [用户]
                                          删掉验证器的绑定和解锁会话（之后在网页上重新扫码绑定）。不写用户就是所有人
python -m roundtable term env-list        列出远程环境
python -m roundtable term env-add --name tokyo --label "东京服务器" --ssh root@203.0.113.7 [--port 22] --key ~/.ssh/keys/tokyo_ed25519 [--cwd /root] [--short 东京]
                                          登记一个远程环境（先真连一次检查）。网页上「新窗口 → 添加远程环境」最后调的就是它
python -m roundtable term env-remove --name tokyo
"""
import argparse
import sys
import time


def main(argv: list[str]) -> int:
    from .. import config as rt
    cmd = argv[0] if argv else "serve"
    if cmd in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if not rt.TERM_URL:
        print("还没有启用终端：在 .env 里设 ROUNDTABLE_TERM_URL（终端在浏览器里的地址，要和 ROUNDTABLE_PUBLIC_URL 不同）。见 docs/terminal.md",
              file=sys.stderr)
        return 2
    from . import auth, config, db, envs
    if cmd == "serve":
        import shutil
        import uvicorn
        if not shutil.which("tmux"):
            print("没有找到 tmux，终端的窗口是开在 tmux 里的，先装上（apt install tmux / brew install tmux）。", file=sys.stderr)
            return 2
        uvicorn.run("roundtable.term.app:app", host=config.HOST, port=config.PORT, log_level="warning", access_log=False,
                    timeout_graceful_shutdown=2, ws_ping_interval=25, ws_ping_timeout=25, ws_max_size=4 * 1024 * 1024)
        return 0
    if cmd == "totp-reset":
        who = auth.reset(argv[1] if len(argv) > 1 else None)
        db.audit("totp_reset", "from cli " + ",".join(who))
        print(f"验证器的绑定已经删掉（{'、'.join(who) or '本来就没有人绑定'}），解锁会话作废。打开网页会重新出二维码。已经连着的终端最多 30 秒后断开。")
        return 0
    if cmd == "status":
        n = db.q1("SELECT COUNT(*) AS n FROM unlocks")["n"]
        print(f"地址 {config.ORIGIN}，监听 {config.HOST}:{config.PORT}，登录方式 {rt.AUTH_MODE}，动态验证码{'开着' if config.TOTP else '没开'}")
        if config.TOTP:
            print(f"绑定了验证器的用户：{'、'.join(auth.enrolled_users()) or '还没有'}；解锁会话 {n} 个")
        print(f"远程环境：{'、'.join(envs.all()) or '没有'}")
        for r in db.q("SELECT ts, kind, detail, ip FROM audit ORDER BY id DESC LIMIT 15"):
            print(" ", time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"])), r["kind"], r["ip"], r["detail"])
        return 0
    if cmd in ("env-list", "env-add", "env-remove"):
        ap = argparse.ArgumentParser(prog=f"python -m roundtable term {cmd}")
        if cmd != "env-list":
            ap.add_argument("--name", required=True)
        if cmd == "env-add":
            ap.add_argument("--label", required=True)
            ap.add_argument("--ssh", required=True)
            ap.add_argument("--key", required=True)
            ap.add_argument("--port", type=int, default=22)
            ap.add_argument("--cwd", default="")
            ap.add_argument("--short", default="")
        a = ap.parse_args(argv[1:])
        if cmd == "env-list":
            for k, e in envs.all().items():
                print(f"{k}\t{e['label']}\t{e['ssh']}{':' + str(e['port']) if e.get('port') else ''}\t{e['cwd']}\t{e['key']}")
            if not envs.all():
                print("还没有远程环境")
            return 0
        try:
            if cmd == "env-add":
                e, notes = envs.add(a.name, a.label, a.ssh, a.key, cwd=a.cwd, port=a.port, short=a.short)
                db.audit("env", f"add {a.name} {e['ssh']}")
                print(f"已登记：{a.name}（{e['label']}），{e['ssh']}，默认目录 {e['cwd']}")
                for n in notes:
                    print("提醒：" + n)
                print("网页上「新窗口」菜单里几秒内会出现它。")
            else:
                if not envs.remove(a.name):
                    print("没有这个环境", file=sys.stderr)
                    return 1
                db.audit("env", f"remove {a.name}")
                print(f"已从清单里拿掉 {a.name}。钥匙文件和那台机器上装的公钥没有动。")
        except envs.EnvError as ex:
            print(f"没成：{ex}", file=sys.stderr)
            return 1
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2
