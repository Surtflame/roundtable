# 登录

圆桌是给**一个人**用的：登录只回答“你是不是部署者本人”，没有注册、没有多用户、没有权限分级。
登录方式用 `.env` 里的 `ROUNDTABLE_AUTH` 选：

| 方式 | 适合 | 要准备什么 |
| --- | --- | --- |
| `none` 不登录 | 只在自己电脑上用；或者服务器上跑、用 SSH 隧道连 | 什么都不用 |
| `password` 一个密码 | 放到公网，但不想碰 Google | 一个域名 + HTTPS，一个够长的密码 |
| `google` Google 账号 | 放到公网，推荐 | 一个域名 + HTTPS，十分钟配 Google |
| `external` 外部登录 | 你已经有一套自己的登录网关（单点登录），想让圆桌接在它后面 | 会写一个二三十行的 Python 文件 |

不管哪种方式，登录检查都做在应用自己里面，每一个请求（页面、静态文件、接口、流式事件）都查，不依赖前面的反向代理。对话的进程不开 WebSocket。

改了 `.env` 要重启服务。任何时候都可以运行 `python -m roundtable check` 看当前配置。

---

## 方式一：不登录（`ROUNDTABLE_AUTH=none`）

默认就是这个。服务只监听 `127.0.0.1`，只有这台机器上的程序连得上。

**在自己电脑上用**：启动后打开 <http://localhost:8050>。

**在服务器上跑，从自己电脑连**：用 SSH 隧道，不用域名也不用证书。

```bash
ssh -N -L 8050:127.0.0.1:8050 you@your-server
# 然后在自己电脑的浏览器里打开 http://localhost:8050
```

这时候谁能进来，取决于谁能 SSH 登录这台服务器。

这个方式下，程序会主动拒绝几种危险的配法：

- `ROUNDTABLE_HOST` 不是回环地址 → 拒绝启动。
- 请求不是从本机直连过来的，或者带着反向代理才会加的头（`X-Forwarded-For` 之类），或者 `Host` 不是 `localhost` / `127.0.0.1` → 拒绝这个请求。

> **不要把 `none` 方式放在反向代理后面。** 反向代理和圆桌在同一台机器上时，所有外面来的请求在圆桌看来都是“本机来的”。上面的检查能挡住常见的代理配置，但如果代理把转发头删掉、把 Host 改写成 localhost，程序就分辨不出来了。要从公网访问，请用下面两种方式之一。

同一台机器上的其他系统用户也能连 `127.0.0.1`。多人共用的机器不要用这个方式。

> **接了 MCP 的话再想一遍。** 这个方式下，能连到本机端口的任何程序、任何本机用户，都能让模型去用你在设置里接的外部工具（见 [mcp.md](mcp.md)）。程序没有为此加限制，自己把握。

---

## 方式二：一个密码（`ROUNDTABLE_AUTH=password`）

没有用户名（反正只有一个人），只有一个密码。

```bash
# .env
ROUNDTABLE_AUTH=password
ROUNDTABLE_PUBLIC_URL=https://chat.example.com
```

```bash
python -m roundtable set-password      # 交互输入两遍，至少 12 位
```

密码不写在 `.env` 里，只把加盐的 scrypt 哈希存进数据目录。改密码就再运行一次 `set-password`：立刻生效，不用重启，之前登录过的浏览器全部下线。

脚本里用可以从标准输入读：`printf '%s\n' "$PW" | python -m roundtable set-password`。

相关命令：

```bash
python -m roundtable logout-all     # 让所有浏览器下线（比如手机丢了）
python -m roundtable unlock         # 解除输错密码造成的锁定
```

### 用密码登录安全吗？

配对了是够用的，但它比 Google 登录弱，弱在哪要心里有数。

做了的事：

- **防暴力猜**：连续输错到第 5 次开始锁，第一次锁 30 秒，之后每错一次翻倍，最长 15 分钟；输对一次清零。锁是全局的，不按 IP 算，所以换 IP、用一堆代理也绕不过去。按最长 15 分钟一次算，一天最多试约 100 次，一个随机的 12 位以上密码猜不出来。
- **密码不落盘**：只存 scrypt 哈希（N=2^15）。数据目录泄露了，对方拿到的是哈希，还得离线硬算；登录 Cookie 在库里也只存哈希，拿到库不等于拿到登录状态。
- **Cookie**：`HttpOnly`、`SameSite=Lax`；配了 HTTPS 时带 `Secure` 并使用 `__Host-` 前缀。
- 写操作都检查请求来源，别的网站没法借你的浏览器替你操作。

没做、做不到的事：

- **没有两步验证**。密码被人知道了（被偷看、在别的网站用过同一个、电脑中了木马）就等于门开了。Google 登录可以借用你 Google 账号上的两步验证和异常登录提醒，这里没有。
- **安全性全在密码本身**。请用密码管理器生成一个随机的长密码，不要用在别处用过的。程序只检查长度，不检查强度。
- **必须有 HTTPS**。没有 HTTPS，密码和 Cookie 在网络上是明文。`ROUNDTABLE_PUBLIC_URL` 不是 `https://` 时启动会提醒；只有在可信的内网 / VPN 里才可以无视它。
- **别人可以把你锁在外面**。因为锁是全局的，有人对着登录页乱试密码，你自己也要等锁过期才能**新**登录。已经登录的浏览器不受影响；在服务器上运行 `python -m roundtable unlock` 可以立刻解锁。如果经常被骚扰，换成 Google 登录，或者在反向代理上加 IP 限制。

一句话：自己用、有 HTTPS、密码是随机生成的，就没问题；如果入口挂在公网上、又希望有两步验证，用 Google 登录。

---

## 方式三：Google 登录（`ROUNDTABLE_AUTH=google`）

你在 Google 那边登记一个“应用”，圆桌把登录这件事交给 Google，然后只放行你名单里的邮箱。密码、两步验证、异常登录提醒都由 Google 管，圆桌拿不到你的 Google 密码。

需要：一个 Google 账号；圆桌已经能通过 `https://你的域名` 访问（见 [deploy.md](deploy.md)）。

### 第 1 步：算出回调地址

回调地址是固定格式：`ROUNDTABLE_PUBLIC_URL` 后面加 `/auth/google/callback`。例如

```
https://chat.example.com/auth/google/callback
```

待会儿要原样填到 Google 那边，协议、域名、端口、大小写、末尾有没有斜杠都必须一模一样。

### 第 2 步：在 Google Cloud 控制台登记

Google 时不时会改控制台的菜单名字，下面是写这份文档时的叫法；找不到就在控制台顶部的搜索框里搜 “OAuth”。这一步不花钱，也不需要绑定付款方式。

1. 打开 <https://console.cloud.google.com/>，用你的 Google 账号登录。顶部的项目选择器里点 **New project**（新建项目），名字随便起，比如 `roundtable`，建好后切换到这个项目。
2. 左侧菜单进 **Google Auth Platform**（老界面叫 **APIs & Services → OAuth consent screen**）。第一次进来会让你填“应用信息”：
   - **App name**：随便，比如 `Roundtable`。登录时 Google 的页面上会显示它。
   - **User support email**、**Contact information**：选/填你自己的邮箱。
   - **Audience**（用户类型）：选 **External**。（**Internal** 只有 Google Workspace 的组织账号才能选，选了就只有组织内的账号能登录。）
   - 同意条款，创建。
3. 进 **Audience** 页面。新建的应用处在 **Testing**（测试）状态，这个状态下**只有“Test users”名单里的账号能登录**。点 **Add users**，把你要用来登录的 Google 邮箱加进去。
   - 就这样一直留在 Testing 状态就可以，不需要点 “Publish app”，也不需要提交 Google 审核。圆桌只向 Google 要“你是谁”（`openid email`），不要任何别的权限，也不保存 Google 的令牌。
4. 进 **Clients**（老界面叫 **Credentials**）→ **Create client**（**Create credentials → OAuth client ID**）：
   - **Application type**：**Web application**。
   - **Name**：随便。
   - **Authorized redirect URIs** → **Add URI**：填第 1 步的回调地址。
   - **Authorized JavaScript origins** 不用填。
   - 创建。
5. 弹出来的窗口里有 **Client ID** 和 **Client secret**，两个都复制下来。Client secret 之后可能不再完整显示，没存下来就在这个客户端的页面里新建一个。

### 第 3 步：填进 `.env`

```bash
ROUNDTABLE_AUTH=google
ROUNDTABLE_PUBLIC_URL=https://chat.example.com
GOOGLE_CLIENT_ID=1234567890-xxxxxxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxx
ROUNDTABLE_ALLOWED_EMAILS=you@gmail.com
```

`.env` 里有密钥，权限收紧：`chmod 600 .env`。

### 第 4 步：检查、重启、登录

```bash
python -m roundtable check
# 输出里有一行：Google 登录回调地址（填到 Google Cloud 控制台里）：https://chat.example.com/auth/google/callback
# 和第 2 步填的对一下
```

重启服务，打开你的域名，点“用 Google 账号登录”。应用在 Testing 状态时，Google 可能会多显示一页“这个应用还在测试 / 未经 Google 验证”的提示，点继续就行——这个应用就是你自己刚登记的。

### 登录不上时

| 现象 | 原因 |
| --- | --- |
| Google 页面显示 **Error 400: redirect_uri_mismatch** | 控制台里填的回调地址和 `check` 打印的不是一模一样。常见的是 http/https 不一致、多了或少了末尾斜杠、`ROUNDTABLE_PUBLIC_URL` 写的不是地址栏里的域名。 |
| Google 页面显示 **Access blocked** / **Error 403: access_denied** | 应用在 Testing 状态，而你登录的账号不在 Test users 名单里。 |
| 回到圆桌后提示“这个 Google 账号不在允许名单里” | 邮箱不在 `ROUNDTABLE_ALLOWED_EMAILS` 里。注意是登录用的那个邮箱，逗号分隔，不分大小写。 |
| 提示“这次登录已经过期或者不是从这个浏览器发起的” | 从点登录到回来超过了 10 分钟，或者浏览器禁了 Cookie，或者回来时的域名和出发时的不一样（比如一个带 `www` 一个不带）。重新点一次。 |
| 提示“和登录服务通信时出了问题” | 服务器连不上 Google（要能访问 `accounts.google.com` 和 `oauth2.googleapis.com`），或者 Client secret 填错了。看服务日志里 `auth:` 开头的行。 |
| 登录后所有按钮都报“跨站请求被拒绝” | `ROUNDTABLE_PUBLIC_URL` 和浏览器地址栏里的地址不一致。 |

### 几个会被问到的问题

**随便一个有 Google 账号的人能登录吗？** 不能。要过两道：Google 那边的 Test users 名单，和圆桌这边的 `ROUNDTABLE_ALLOWED_EMAILS`。即使你把 Google 应用发布成了正式状态，第二道名单仍然在；没填名单时程序拒绝启动。

**把账号从名单里拿掉之后呢？** 改 `.env`、重启，那个账号已有的登录立刻失效（每个请求都会重新对一遍名单）。也可以运行 `python -m roundtable logout-all`。

**名单里能写多个邮箱吗？** 技术上能，但所有人看到的是同一份对话记录，用的是同一套模型订阅和密钥。订阅账号给别人用通常违反服务条款，见 [backends.md](backends.md) 的“规则”一节。启动时会就此提醒。

**实现上的细节**（给想审代码的人）：授权码流程 + PKCE（S256）+ `state` + `nonce`；`state` 同时绑定在一个短时 Cookie 上，别人发来的回调链接在你的浏览器里不成立；每个 `state` 只能用一次。ID Token 是服务器通过 TLS 直接从 Google 的令牌接口取回的，按 OpenID Connect Core 3.1.3.7 的规定这种情况可以用 TLS 的服务器验证代替签名验证，所以没有再验 JWT 签名；`iss`、`aud`、`azp`、`exp`、`nonce`、`email_verified` 都检查。代码在 `roundtable/auth.py`，三百多行。

**能换成别的登录服务吗？** `ROUNDTABLE_OIDC_ISSUER` 可以指向别的标准 OpenID Connect 提供方（要支持 discovery，并在 ID Token 里给出 `email` 和 `email_verified`）。只对着 Google 的规范实现过，别家没有逐个测。

---

## 方式四：外部登录（`ROUNDTABLE_AUTH=external`）

大多数人用不到这一节。它是给这种情况准备的：你的服务器上已经有一套登录网关——反向代理在每个请求前先问一下“这个人登录了没有”（Caddy 的 `forward_auth`、nginx 的 `auth_request`、oauth2-proxy、Authelia、Cloudflare Access 之类），好几个自己的站共用一个登录。你想让圆桌也接在它后面，不想再多一套登录。

### 为什么不是“前面挡住了就行”

最省事的做法是圆桌什么都不查，全靠代理。但这样有两个洞：

- **绕过代理**：应用监听在本机的某个端口上。同一台机器上的别的程序、配错的防火墙、另一个反向代理，只要能直接连到这个端口，就完全不用登录。
- **网关配错**：网关那边手一滑把别的站的访问权限也给了某个人，或者规则写宽了，圆桌自己毫不知情。

所以圆桌的规矩是：**不管前面有没有人挡，应用自己每个请求都核对一遍**。问题是“怎么核对”因网关而异——有的可以去查网关的会话库，有的是验一个签名的令牌，有的是认一个只有代理知道的密钥——没法在圆桌里写死。于是做成了一个口子：你写一个小文件告诉圆桌怎么核对，这个文件放在**仓库外面**，圆桌每个请求都调它。

这样做还有一个好处：你的部署细节（网关在哪、库在哪、怎么认人）都在这个文件和 `.env` 里，圆桌的代码一行不用改，升级时直接拉新版本。圆桌作者自己的部署就是这么接的。

### 怎么配

```
# .env
ROUNDTABLE_AUTH=external
ROUNDTABLE_AUTH_PLUGIN=/etc/roundtable/auth.py
# 可选：再加一道名单。设了的话，文件返回的用户还必须在这里面
ROUNDTABLE_ALLOWED_EMAILS=you@example.com
```

那个文件只要有一个函数：

```python
def authenticate(request) -> str | None:
    # request = {"site": "chat" 或 "term", "headers": {小写的请求头: 值}, "cookies": {名字: 值}, "client": 对端地址}
    ...
```

- 返回一个字符串（用户的标识，一般是邮箱）＝ 放行。
- 返回 `None` ＝ 没登录（401）。
- 抛 `PermissionError("原因")` ＝ 登录了，但不许进（403）。
- 抛任何别的异常 ＝ 当作登录服务出了问题（503）。**出错一律不放行。**
- `site` 是 `chat`（对话）或 `term`（[网页终端](terminal.md)），你可以对终端用更严的规则。
- 可选：模块里放一个 `LOGIN_URL = "https://…"`，没登录的人直接打开页面时会被送到那里（一般到不了这一步，代理先拦了）。

`deploy/auth_plugin.example.py` 是一个能用的例子（代理转发“登录的是谁”的请求头，外加一个只有代理知道的密钥）。`.env` 里的每一项这个文件都能用 `os.environ` 读到，所以它自己的设置也可以写在 `.env` 里。

要注意的：

- **这个文件里的代码决定谁能进来，而且是以服务的身份运行的。** 它的主人必须是运行服务的用户或 root，不能让别人可写，否则圆桌拒绝启动。别从不信任的地方拿这种文件。
- 每个请求都会调一次（事件流和终端的长连接是每 30 秒一次），所以它要快：查本机的库、验签名可以，别在里面发网络请求。
- 这种方式下圆桌没有登录页、不发登录 Cookie，“退出登录”也在你的网关那边。跨站请求的检查（`Origin`、自定义请求头）照旧，和别的方式一样。
- 开了网页终端的话，终端不需要“递票”，它和对话一样直接问这个文件；动态验证码那一道照旧。
- `python -m roundtable check` 会试着加载这个文件，报告能不能用。

## 登录状态

- 登录一次管 `ROUNDTABLE_SESSION_DAYS` 天（默认 30），到期重新登录。
- 页面左下角的设置里可以退出登录，只退当前浏览器。
- `python -m roundtable logout-all` 让所有浏览器下线。开着的页面会在半分钟内发现自己被下线。
- 换登录方式（改 `ROUNDTABLE_AUTH`）后，旧方式发的登录会失效。
