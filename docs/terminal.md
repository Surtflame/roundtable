# 网页终端（可选）

圆桌自带一个可选的网页终端：在浏览器里开服务器上的终端窗口，主要是为了在网页上用 Claude Code 和 Codex 这类命令行工具。左边栏顶上多一排「对话 | 终端」，点一下切过去，两边都一直开着，切换不重新加载。

**启用它 = 让能登录的人在浏览器里拿到这台服务器的 shell。** 先读完“安全”一节再决定开不开。默认不开。

## 它做什么

- **多窗口**：每个窗口背后是 tmux 里一个一直在跑的终端。关掉浏览器、断网、换设备都不影响，回来接着用。
- **一键开 Claude Code / Codex**，也可以带上 Claude Code 的 Remote Control（`claude --remote-control`），这样同一个对话在 Claude 的手机和桌面应用里也能看到、接着聊。
- **历史对话**：列出这台机器上 Claude Code 和 Codex 存下来的会话，点一下就在新窗口里接着聊（`claude --resume` / `codex resume`）。可以置顶、隐藏；程序调出来的会话（脚本里的 `claude -p` 之类）默认收在“已隐藏”里。
- **正在别处跑着的对话**：如果一个对话正开在这台机器的另一个 tmux 会话里（比如你从 SSH 开的 `claude --remote-control`），点它是直接接到那个终端上，不会另起一份。
- **文件管理**：看目录、编辑文本、上传、下载、新建、改名、删除。
- **远程环境**：把别的机器接进来，新窗口可以直接开在那台机器上（背后是 SSH + 那台机器自己的 tmux）。「新窗口 → 添加远程环境」会开一个 Claude Code 对话，问你地址和登录方式，然后替你把钥匙和登记配好。
- 手机上能用（有一排 Esc / Tab / Ctrl / 方向键），但主要是给电脑用的。

## 启用

终端是**另一个进程**，放在**另一个来源**（另一个子域名，或者本机的另一个端口）。

需要：`tmux`（3.2 以上）。远程环境还要本机有 `ssh`。

### 只在本机用

`.env`：

```
ROUNDTABLE_TERM_URL=http://localhost:8060
```

开两个进程：

```bash
python -m roundtable          # 对话，http://localhost:8050
python -m roundtable term     # 终端，http://localhost:8060
```

`ROUNDTABLE_AUTH=none` 时终端和对话一样只接受本机直连，默认不要动态验证码。

### 放在服务器上

1. 给终端准备一个**和对话不同的域名**，比如对话是 `chat.example.com`，终端用 `term.example.com`，DNS 指到同一台机器。
   （用 sslip.io 这类“IP 当域名”的服务也行：`term.203-0-113-7.sslip.io`。）
2. `.env`：
   ```
   ROUNDTABLE_TERM_URL=https://term.example.com
   ```
3. 反向代理把这个域名转给 `127.0.0.1:8060`，**要支持 WebSocket**。Caddy 的写法见 `deploy/Caddyfile.example`（Caddy 默认就支持）；nginx 见 `deploy/nginx.conf.example` 里终端那一段。
4. 两个 systemd 服务：`deploy/roundtable-tmux.service`（tmux 本身）和 `deploy/roundtable-term.service`（终端的网页服务）。分成两个是为了重启网页服务时窗口不受影响。
5. 重启对话的服务（它要知道终端的地址，才会显示「对话 | 终端」）。
6. 打开对话页面，点「终端」。第一次会让你用手机上的验证器（Google Authenticator、1Password 之类）扫码绑定。

配得对不对：`python -m roundtable check` 和 `python -m roundtable term status`。

### 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `ROUNDTABLE_TERM_URL` | 空（不启用） | 终端在浏览器里的地址。必须和 `ROUNDTABLE_PUBLIC_URL` 不同来源 |
| `ROUNDTABLE_TERM_HOST` / `ROUNDTABLE_TERM_PORT` | `127.0.0.1` / `8060` | 终端的进程监听在哪 |
| `ROUNDTABLE_TERM_USERS` | 空 | `google` 登录方式下，只让名单里的这几个邮箱进终端。空 = 能登录的都能进 |
| `ROUNDTABLE_TERM_TOTP` | 要登录时 `on`，本机模式 `off` | 动态验证码这一道开不开 |
| `ROUNDTABLE_TERM_UNLOCK_HOURS` / `ROUNDTABLE_TERM_IDLE_MIN` | `12` / `120` | 输一次验证码最多管多久；多久没操作自动锁上 |
| `ROUNDTABLE_TERM_CWD` | 家目录 | 新窗口默认开在哪 |
| `ROUNDTABLE_TERM_PROTECT` | 空 | 逗号分隔的目录：文件管理里不许改、不许删、不许往里传（终端里照样能动，只是防手滑） |
| `ROUNDTABLE_TERM_MAX_UPLOAD_MB` | `2048` | 文件管理单个上传的上限 |

## 安全

终端里的 shell 是以**运行服务的那个系统用户**的身份跑的。这个用户能做什么，进了终端的人就能做什么；它要是能免密 `sudo`，那就是 root。

### 两道门

1. **圆桌的登录**。终端和对话是两个来源，对话那边的登录 Cookie 终端看不到，所以登录是“递票”过来的：已经登录的对话页面向它的后端要一张一次性票据（60 秒作废、只能用一次、库里只存哈希），浏览器带着票据去终端，换成终端自己的会话 Cookie。之后终端的每个请求都回库里查这个会话还在不在、这个人现在还许不许登录。
2. **动态验证码（TOTP）**。每个用户各自绑定各自的验证器。验证通过后发一个只给终端这个站的 Cookie，默认最长 12 小时、闲置 2 小时失效。连错 5 次锁 5 分钟，同一个验证码不能用第二次。密钥在服务器的 `<数据目录>/term/totp.json`（权限 600）；绑定之后网页上不能重绑，要重置得在服务器上运行 `python -m roundtable term totp-reset`。

密码登录方式本身没有二次验证，所以用密码登录又开终端的话，这一道动态验证码就是唯一的第二重保护，不建议关。

### 为什么要放在另一个来源

对话页面要显示模型输出和从网页上抓来的内容。那里虽然做了清洗和内容安全策略，但万一有疏漏、被注入了脚本，脚本在对话的来源下运行——隔着来源，它读不到终端页面的任何内容，也没法替你往终端里敲键盘。所以配置里强制两个地址不同来源，写成一样的会拒绝启动。

为了切换不重新加载，两个站互相把对方放在一个铺满窗口的 iframe 里（内容安全策略里只允许对方那一个来源）。这带来一点新的风险：对话那一页如果被注入了脚本，可以在终端上面盖一层东西，骗你往下面点或者粘贴。它仍然读不到终端内容。

### WebSocket

终端的输入输出走 WebSocket。握手之后就是一条长连接，不会再经过普通请求的那些检查，所以单独做了几件事：

- 握手时检查 `Origin` 必须是终端自己的地址（浏览器发起 WebSocket 会自动带 Cookie，而且不受同源策略限制）。
- 握手的地址里要带一张一次性票据，票据得先用一个普通请求（过得了跨站检查的）来换，30 秒内用掉。
- 连上之后每 30 秒复查一次登录和解锁状态，失效就断开；锁定、退出时立刻断开。

### 别的

- 文件管理只在图片上做网页预览，别的文件一律当附件下载，不会在终端的域名下把文件内容当网页渲染。
- 终端里复制的内容可以送到浏览器的剪贴板；反过来，终端里的程序**不能**读你的剪贴板。
- 操作记录（解锁、锁定、开关窗口、文件改动、登记环境）记在 `<数据目录>/term/term.db` 的 `audit` 表里，`python -m roundtable term status` 能看最近几条。终端里敲了什么不记录。
- 远程环境：每个环境用一把专用的 SSH 钥匙（建议放在 `~/.ssh/keys/`），清单在 `<数据目录>/term/envs.json`，里面没有密码。加了远程环境之后，能进终端的人也就能进那台机器。“添加远程环境”的对话里如果你把密码告诉了 Claude Code，密码会留在那次对话的记录里（`~/.claude` 下）。
- 多个用户：每个人有自己的验证器，但进来之后是同一个系统用户、同一批窗口，互相看得到。和对话一样，这不是多用户系统。

### 测过的和没测过的

测过（Linux，Chromium）：三种登录方式下的递票登录；验证器的绑定、解锁、锁定、重放和连错锁定；WebSocket 的来源检查、票据、定期复查；窗口的开关和断线重连；文件管理；历史对话的列出、接着聊、置顶、隐藏；接到别的 tmux 会话上；远程环境的开窗口、接入、关闭和历史对话；两个站互相切换。

没测过：macOS；Safari 和 iOS（iframe 嵌不进去时会在几秒后退回整页跳转）；nginx 的示例配置；Codex 的历史对话只对着 Codex CLI 0.16x 的本地库格式验证过；“添加远程环境”的对话只验证了它会按要求提问和调用登记命令，没有对着各种系统的远程机器逐一走完。

## 远程环境

「新窗口」菜单最下面的“添加远程环境…”会在本机开一个 Claude Code 对话（需要这台机器上的 Claude Code 已经登录），它会：

1. 问你那台机器的地址、端口、用户名、怎么登录（密码、现成的私钥，或者都不给——它生成公钥，你自己装上去）；
2. 生成一把专用的钥匙，装到那台机器上，验证能免密登录；
3. 检查那边有没有 `tmux`（必须）、`python3`（列历史对话要用）、`claude`；
4. 运行 `python -m roundtable term env-add …` 登记。几秒后「新窗口」菜单里就多出那台机器。

也可以不用 AI，自己配好钥匙后直接登记：

```bash
python -m roundtable term env-add --name tokyo --label "东京服务器" --ssh root@203.0.113.7 --key ~/.ssh/keys/tokyo_ed25519 --cwd /root
python -m roundtable term env-list
python -m roundtable term env-remove --name tokyo
```

窗口开在那台机器自己的 tmux 里（单独的套接字 `tmux -L roundtable`），所以这边重启不影响它；那台机器重启才会没。那边的 Claude Code 历史对话也会列出来，带上环境的名字。文件管理只管本机。

要彻底断开一台机器：`env-remove`，再去那台机器的 `~/.ssh/authorized_keys` 里删掉对应的那一行公钥。

## 常见问题

- **点「终端」没反应或者转回了对话**：看终端的进程起来没有（`journalctl -u roundtable-term`），`ROUNDTABLE_TERM_URL` 和浏览器里实际的地址是不是一致。
- **终端一直“正在连接…”**：多半是反向代理没有转发 WebSocket。
- **换手机了、验证器没了**：在服务器上 `python -m roundtable term totp-reset`，再打开网页重新扫码。
- **重启之后窗口都没了**：机器重启或者 tmux 的服务重启，窗口就没了（tmux 本身不持久化）。对话有存档，从“历史对话”里接着聊。
- **从 SSH 里接进同一个窗口**：`tmux -L roundtable attach -t <窗口名>`（窗口标签的提示里有完整命令）。
