# 安装与部署

圆桌是一个 Python 进程 + 一个 SQLite 文件，没有别的组件。前端不需要构建。

先说清楚测试范围：作者只在 **Linux** 上跑过——日常用的是 Ubuntu 24.04 + Python 3.12；另外在一台全新的 Ubuntu 26.10（Python 3.14，1 核 1G 内存的小机器）上照这份文档从头装过一遍（装到 HTTPS 能访问、登录页正常、两个命令行工具检测通过为止；写这句话时那台机器上还没有登录模型账号，没有跑过真实回答）。macOS 理论上能跑（Codex 外面那层 bubblewrap 沙箱没有），Windows 没有试过，建议用 WSL。

## 1. 需要什么

- Python 3.11 或更新
- 至少一个模型后端（可以先装好圆桌再配）：Claude Code、Codex CLI、CodeBuddy Code、OpenRouter 密钥，见 [backends.md](backends.md)
- Linux 上建议装 bubblewrap（`sudo apt install bubblewrap`），给 Codex 多一层沙箱
- 要从别的设备访问的话：一个域名和 HTTPS（下面有 Caddy / nginx 的例子）

## 2. 安装

建议用一个**专门的普通用户**来跑，不要用 root，也尽量不要用你日常放着各种密钥的那个账号——原因见 [security.md](security.md)。

```bash
git clone <这个仓库的地址> roundtable
cd roundtable
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

`requirements.txt` 只写了直接依赖的最低版本；`requirements.lock` 是作者测试时的完整版本组合，想要一模一样的环境就用它：`.venv/bin/pip install -r requirements.lock`。

## 3. 配置

所有配置都在 `.env` 里，每一项在 [.env.example](../.env.example) 里都有说明。最少要决定两件事：

1. **怎么登录**（`ROUNDTABLE_AUTH`）——见 [login.md](login.md)。
   - 只在本机用：什么都不用改，默认 `none`，只监听 `127.0.0.1`。
   - 要从别处访问：`password` 或 `google`，并且把 `ROUNDTABLE_PUBLIC_URL` 设成浏览器里看到的地址（`https://chat.example.com`）。
2. **用哪些模型**——见 [backends.md](backends.md)。

`.env` 的写法注意：一行一个 `名字=值`，**值后面不要跟注释**（`#` 会被当成值的一部分），注释单独写一行。

改完检查一遍：

```bash
.venv/bin/python -m roundtable check
```

它会告诉你：对外地址、监听地址、登录方式、数据目录，Google 登录要填的回调地址，以及各个后端能不能用、为什么不能用。配置不安全时（比如不要登录却监听了公网地址）它会直接拒绝启动并说明原因。

## 4. 启动

```bash
.venv/bin/python -m roundtable
```

然后浏览器打开 `http://localhost:8050`。

机器在远处、又不想配域名的话，用 SSH 隧道就够了（登录方式保持 `none`）：

```bash
ssh -L 8050:127.0.0.1:8050 你@服务器
# 然后在自己电脑上打开 http://localhost:8050
```

## 5. 常驻运行（systemd）

[deploy/roundtable.service](../deploy/roundtable.service) 是一个例子。把里面的用户名和路径改成你的，然后：

```bash
sudo cp deploy/roundtable.service /etc/systemd/system/roundtable.service
sudo systemctl daemon-reload
sudo systemctl enable --now roundtable
journalctl -u roundtable -f        # 看日志
```

注意服务要用**登录过 Claude Code / Codex / CodeBuddy 的那个用户**来跑，命令行工具的登录状态在那个用户的家目录里。

重启服务时正在生成的回答会被打断（界面上会标出来，点“重新生成”即可）；已经生成的内容不会丢。

## 6. HTTPS 和反向代理

只要不是纯本机使用，就应该走 HTTPS：密码和登录 Cookie 不能在网络上明文传输。圆桌自己不处理证书，前面放一个反向代理。

**Caddy**（自动申请证书，最省事）——[deploy/Caddyfile.example](../deploy/Caddyfile.example)：

```
chat.example.com {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8050 {
		flush_interval -1
	}
}
```

**nginx**——[deploy/nginx.conf.example](../deploy/nginx.conf.example)。

两者都要注意的一点：圆桌用 SSE（服务器推送事件）把生成中的文字推给浏览器，**反向代理不能缓冲响应**。Caddy 是 `flush_interval -1`，nginx 是 `proxy_buffering off`，再加上足够长的读超时。漏了这一条的症状是：回答不是一个字一个字出来，而是憋很久之后一下子全出来，或者界面频繁显示“连接断开”。

圆桌不用 WebSocket，代理不需要为它做特别配置。

### 没有域名，只有一个公网 IP

证书和 Google 登录都要一个域名：Google 的回调地址不接受裸 IP，也不接受 `http://`（`localhost` 除外）。没有自己的域名时，可以用“IP 即域名”的公共解析服务，比如 [sslip.io](https://sslip.io) 或 nip.io：`203-0-113-7.sslip.io` 这个名字会解析到 `203.0.113.7`，不用注册、不用配置。把它当成域名用就行：

```
203-0-113-7.sslip.io {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8050 {
		flush_interval -1
	}
}
```

`.env` 里写 `ROUNDTABLE_PUBLIC_URL=https://203-0-113-7.sslip.io`，Google 那边的回调地址写 `https://203-0-113-7.sslip.io/auth/google/callback`。Caddy 给这种名字申请 Let's Encrypt 证书实测是成的。

要知道的代价：解析靠的是别人运营的免费服务。它停了，你的地址就打不开；运营它的人（或者劫持了它的人）理论上能把这个名字指到别的机器上去。换了 IP 也等于换了地址，三处都要跟着改。长期用还是买一个自己的域名。

圆桌另外还会在 `127.0.0.1` 上开一个内部端口（MCP 网关，默认端口随机，见 [mcp.md](mcp.md)），只给它自己启动的命令行工具用。**不要在反向代理里转发它，也不要在防火墙上放行它。**

配了反向代理之后：

- `ROUNDTABLE_HOST` 保持 `127.0.0.1`（只让代理连它，不要直接暴露端口）
- `ROUNDTABLE_PUBLIC_URL` 写 `https://你的域名`
- `ROUNDTABLE_AUTH` 必须是 `password` 或 `google`。**`none` 不能放在反向代理后面**——那等于谁都能进。圆桌在 `none` 方式下看到代理转发的请求头会直接拒绝，但不要依赖这一点。
- 上传大小：圆桌默认单个文件 50 MB（`ROUNDTABLE_MAX_UPLOAD_MB`），nginx 的 `client_max_body_size` 要不小于它。

### 网页终端（可选）

想在浏览器里用服务器上的终端（Claude Code、Codex），见 [terminal.md](terminal.md)：另一个域名、另一个进程、两个 systemd 服务。默认不开。

## 7. 数据在哪，怎么备份

全部在数据目录里（默认是项目下的 `data/`，可以用 `ROUNDTABLE_DATA_DIR` 改）：

| 路径 | 内容 |
|---|---|
| `roundtable.db`（以及 `-wal`、`-shm`） | 对话、设置、登录会话、密码哈希、MCP 服务器的令牌（明文） |
| `files/` | 你上传的附件、模型画的图 |
| `runs/`、`empty/` | 运行时的临时目录，不用备份 |
| `term/` | 开了网页终端才有：验证器密钥（`totp.json`）、远程环境清单、置顶和隐藏的记录、操作记录 |

数据目录的权限会被设成只有运行用户可读（700）。里面的内容**没有加密**，能读这个目录的人就能读你的全部对话。

备份：数据库在运行时不要直接 `cp`（可能拷到写了一半的状态），用 SQLite 的在线备份：

```bash
.venv/bin/python - <<'EOF'
import sqlite3
src = sqlite3.connect("data/roundtable.db")
dst = sqlite3.connect("backup-roundtable.db")
src.backup(dst)
dst.close(); src.close()
EOF
```

然后把 `backup-roundtable.db` 和 `files/` 目录一起存到别处。接了 MCP 的话，备份里有那些服务的令牌，按存放密钥的标准保管。恢复就是停掉服务、把这两样放回数据目录、再启动。

单个对话可以在界面里导出成 Markdown。

## 8. 升级

```bash
cd roundtable
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart roundtable
```

升级前先备份数据目录。程序启动时会建缺少的表、给已有的表补上新增的列（带默认值，不动已有数据），除此之外**没有别的数据库迁移机制**：以后哪个版本要是改了已有的表结构，会在更新说明里写清楚怎么处理。

Claude Code 和 Codex 是各自独立升级的。圆桌每次启动都会重新检测它们支持哪些参数；如果新版本把圆桌用来做隔离的开关改没了，对应的后端会被标成“未启用”并写明原因，而不是带着缺口继续跑。升级它们之后跑一次 `python -m roundtable check` 看看。

## 9. 常见问题

**启动报“配置有问题”**：照着提示改 `.env`。最常见的是 `ROUNDTABLE_AUTH=none` 配了非本机的 `ROUNDTABLE_HOST`。

**页面能打开，但提交任何操作都报 403**：`ROUNDTABLE_PUBLIC_URL` 和浏览器地址栏里的地址不一致（协议、域名、端口都要一样）。圆桌用它来拒绝跨站请求。

**模型列表是空的**：一个后端都没配好。运行 `python -m roundtable check` 看每个后端的状态。

**Claude / Codex 在终端里能用，在圆桌里说没登录**：服务是用另一个系统用户跑的，或者 `HOME` 不对。命令行工具的登录状态是跟着用户走的。

**回答憋很久才一次性出来**：反向代理在缓冲，见上面第 6 节。

**机器内存小（1–2G）**：每个正在生成的 Claude / Codex 回答是一个独立的命令行进程，一个要占几百 MB 内存。一次问好几个模型、再加互评，小机器会吃紧甚至被系统杀进程。加上交换分区，并把 `.env` 里的 `ROUNDTABLE_CONCURRENCY_CLAUDE`、`ROUNDTABLE_CONCURRENCY_CODEX` 调小（比如 2）：多出来的回答会排队，不会丢。

**需要走代理才能访问模型服务**：在 `.env` 里设 `HTTPS_PROXY`（和需要的 `NO_PROXY`），圆桌自己的请求和两个命令行工具都会用它。
