# 模型后端：怎么配，以及各家的规则

圆桌自己不带任何模型，也不带任何账号。它调用你**自己装的、自己登录的**这几样东西：

| 后端 | 圆桌怎么调用 | 钱怎么算 |
| --- | --- | --- |
| Claude Code | 在服务器上运行官方的 `claude -p` 命令 | 你的 Claude 订阅额度，或者 Anthropic API 密钥按量 |
| Codex | 在服务器上运行官方的 `codex exec` 命令 | 你的 ChatGPT 订阅额度，或者 OpenAI API 密钥按量 |
| CodeBuddy | 在服务器上运行官方的 `codebuddy -p` 命令 | 你的 CodeBuddy / WorkBuddy 订阅额度（积分），或者它的 API 密钥 |
| OpenRouter | 直接发 HTTPS 请求 | OpenRouter 余额，按量 |

至少配一个，配几个都行。没配的后端在界面里不出现；没配成功的，原因写在左下角设置的“用量”里，也可以运行 `python -m roundtable check` 看。

> **先读这一段。**
> 用订阅账号（而不是 API 密钥）驱动这样一个应用，是否符合 Anthropic、OpenAI 和腾讯的条款，**几家都没有给出明确的说法**，本文后半部分的“[规则](#规则)”把能找到的原文和没说清的地方都列出来了。
> 用不用、怎么用，由部署的人自己判断、自己负责。因为使用本项目导致账号被限流、被暂停、被封禁、被额外扣费或者任何其他后果，本项目的作者和贡献者不承担任何责任（另见 [LICENSE](../LICENSE) 的免责条款）。
> 如果你不想碰任何灰色地带：每个后端都用 **API 密钥**，按量付费。这是各家文档明确写了给程序用的方式。

---

## 一个前提：用哪个系统用户

`claude`、`codex`、`codebuddy` 的登录状态存在**系统用户的家目录**里（`~/.claude`、`~/.codex`、`~/.local/share/CodeBuddyExtension`）。圆桌以哪个用户运行，就用哪个用户的登录状态。所以：

- 下面的登录命令，要用**运行圆桌的那个用户**去执行。用 systemd 的话就是服务文件里的 `User=`。
- 不要用 root 运行。
- 建议给圆桌单独建一个系统用户，或者至少别用一个平时放了很多别的东西的账号，理由见 [security.md](security.md)。

---

## Claude Code

### 安装

官方安装说明：<https://code.claude.com/docs/en/setup>。常见的装法：

```bash
curl -fsSL https://claude.ai/install.sh | bash       # 官方安装脚本，装到 ~/.local/bin/claude，不需要 Node.js
# 或者：npm install -g @anthropic-ai/claude-code    # 需要 Node.js 18+
claude --version
```

用**跑圆桌的那个系统用户**来装和登录。安装脚本可能提示 `~/.local/bin` 不在 PATH 里：圆桌自己会去那里找，不影响；只是你在终端里敲 `claude` 之前要先把它加进 PATH。

圆桌需要比较新的版本（要有 `--restricted` 或 `--setting-sources` 参数来做隔离）。版本太旧时这个后端会显示为不可用，并说明缺什么。

装在了 PATH 以外的地方，就在 `.env` 里写 `ROUNDTABLE_CLAUDE_BIN=/完整/路径/claude`。

### 登录：三选一

**A. 订阅账号登录**（Pro / Max / Team / Enterprise 等）

```bash
claude auth login          # 会给一个网址，在任意浏览器里打开、登录、把授权码贴回来
claude auth status         # 确认
```

用量算在订阅额度里，和你平时用 Claude Code 共用同一份额度（有 5 小时窗口和每周上限，具体见你的套餐）。

**B. 长期令牌**（没法在服务器上开浏览器、或者不想把完整登录状态放在服务器上时）

```bash
claude setup-token         # 在能开浏览器的机器上运行，得到一个一年有效的令牌，需要订阅
```

然后在服务器的 `.env` 里：

```bash
ROUNDTABLE_CLI_PASS_ENV=CLAUDE_CODE_OAUTH_TOKEN
CLAUDE_CODE_OAUTH_TOKEN=粘贴上一步的令牌
```

本质上仍然是订阅账号，规则和 A 一样。

**C. API 密钥**（按量付费，规则上最清楚）

在 Claude Console（<https://platform.claude.com/>）建一个密钥，然后在 `.env` 里：

```bash
ROUNDTABLE_CLI_PASS_ENV=ANTHROPIC_API_KEY
ANTHROPIC_API_KEY=sk-ant-...
```

按官方文档，`-p` 模式下只要有这个变量就用它，不走订阅。钱按 API 价格从 Console 余额里扣，建议在 Console 里设消费上限。

`ROUNDTABLE_CLI_PASS_ENV` 的作用：圆桌启动命令行工具时，默认只给它 `HOME`、`PATH`、`LANG` 和代理设置，别的环境变量一概不传；要传的在这里点名。

### 模型清单

默认列出 Claude Code 自带的几个模型别名（`fable`、`opus`、`sonnet`、`haiku`），别名具体指向哪个版本由你装的 Claude Code 决定；你的套餐不包含的模型选了会报错。

**版本号是自动显示的。** Claude Code 每次运行都会报出别名实际解析成了哪个模型（比如 `opus` → `claude-opus-4-5-…`），圆桌把它记下来：

- 某个别名**第一次回答之后**，它的名字就从“Claude Opus”变成“Claude Opus 4.5”，开着的页面不用刷新。在那之前圆桌不知道版本，也不会为了知道而替你发一次请求。自动起标题用的是 `haiku`，所以它通常最先带上版本号。
- Claude Code 升级后别名指向了新版本，下一次回答之后名字跟着变。
- 每个回答的卡片上显示的是**它当时实际用的版本**，以后别名换了指向，旧回答的名字不变。升级到这个功能之前的旧回答没有记录，名字不带版本号。
- 设置的“用量”里，同一个别名的不同版本分开统计。

想自己定清单：

```bash
ROUNDTABLE_CLAUDE_MODELS=opus,sonnet,haiku
```

每一项是别名或完整的模型 id，用逗号隔开。只写别名时名字和版本号照上面的办法自动来；写的是完整的模型 id（`claude-opus-4-5-20251101`）时，名字直接从 id 里读出来（“Claude Opus 4.5”），永远指向这一版。

也可以用 `别名=显示名` 自己起名字，比如 `opus=主力`。自己起的名字圆桌原样使用、不加版本号，别名实际指向哪个模型会写在选模型面板里那一项的说明里。

### 圆桌具体是怎么调用的

每个回答启动一个 `claude -p` 进程，用完就退出。参数的要点（完整的在 `roundtable/backends.py`）：

```
claude -p --model <别名> --input-format stream-json --output-format stream-json --verbose
       --system-prompt <圆桌的系统提示词>
       --restricted                      （旧版本没有就用 --setting-sources ""）
       --strict-mcp-config --no-session-persistence --disable-slash-commands
       --tools "WebSearch,WebFetch"      （没开联网时是 --tools ""）
       --mcp-config <临时文件> --allowedTools ...   （只在圆桌里接了 MCP 时才有）
```

- 用的是**没有改动过的官方程序**，圆桌不读取、不保存、不转发你的 Claude 登录凭据，也不自己去连 Anthropic 的接口。
- 工作目录是一个空目录；不加载你的 MCP 服务器、设置文件、技能、钩子；不写会话历史。
- 模型能用的工具只有联网搜索和抓网页（开了“联网”时），没有命令行、没有读写文件。
- 在圆桌的设置里接了 MCP 并勾了工具时（见 [mcp.md](mcp.md)），再加上那些工具：圆桌给它一份只指向本机网关的临时配置，令牌不在里面。

---

## Codex

### 安装

官方仓库和安装说明：<https://github.com/openai/codex>。常见的装法：

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh    # 官方安装脚本（独立安装版），不需要 Node.js
# 或者：npm install -g @openai/codex
codex --version
```

装在了 PATH 以外的地方，写 `ROUNDTABLE_CODEX_BIN=/完整/路径/codex`；不想用就写 `ROUNDTABLE_CODEX_BIN=off`。

Linux 上建议同时装 bubblewrap，圆桌会把 Codex 关在里面运行（见 [security.md](security.md)）：

```bash
sudo apt install bubblewrap        # Debian / Ubuntu
sudo dnf install bubblewrap        # Fedora
```

没有 bubblewrap（比如 macOS）也能用，只是少一层隔离，启动日志和设置页会提醒。想强制“没有沙箱就不启用 Codex”，设 `ROUNDTABLE_CODEX_SANDBOX=bwrap`。

装了 bubblewrap，`python -m roundtable check` 却说“bwrap 沙箱里启动 codex 失败”（报错里常带 `setting up uid map: Permission denied`）：多半是 Ubuntu 的 AppArmor 不让普通用户建用户命名空间（`sysctl kernel.apparmor_restrict_unprivileged_userns` 是 1），而系统里又没有给 `bwrap` 放行的 AppArmor 规则。Ubuntu 24.04 会遇到；比较新的 Ubuntu 自带放行规则（`/etc/apparmor.d/bwrap-userns-restrict`），不用管。两个办法：

```bash
# 办法一：整机关掉这项限制（简单，但对机器上所有程序都放开了）
echo 'kernel.apparmor_restrict_unprivileged_userns = 0' | sudo tee /etc/sysctl.d/99-userns.conf
sudo sysctl --system
# 办法二：只给 bwrap 加一条 AppArmor 规则（参考新版 Ubuntu 的 bwrap-userns-restrict），这里不展开
```

> 实测过的组合是 Linux + 独立安装版的 Codex + bubblewrap。npm 安装版套沙箱的代码写了但没有在真机上测过；macOS 没有测过。遇到问题先设 `ROUNDTABLE_CODEX_SANDBOX=off` 确认是不是沙箱的原因，并欢迎提 issue。

### 登录：二选一

**A. ChatGPT 订阅登录**

```bash
codex login                    # 本机有浏览器
codex login --device-auth      # 服务器上没有浏览器：给一个网址和一串码，在别的设备上输入
codex login status
```

用量算在 ChatGPT 套餐的 Codex 额度里。

**B. API 密钥**（按量付费）

```bash
printenv OPENAI_API_KEY | codex login --with-api-key
```

OpenAI 的文档明确把 API 密钥作为“程序化 / 自动化使用 Codex CLI”的推荐方式。

登录状态在 `~/.codex/auth.json`（或者 `CODEX_HOME` 指的目录）。OpenAI 的文档要求像对待密码一样对待这个文件。

### 模型清单

读的是 Codex 自己缓存的模型清单（`~/.codex/models_cache.json`），你的账号能用哪些就列哪些。刚装好、还没运行过时这份缓存还不存在，圆桌先用 `codex debug models` 给出的、这个版本 CLI 自带的清单顶上（不看账号，里面可能有你的账号用不了的模型）；用过一次之后就换成缓存里的那份。很旧的 Codex 没有这个命令，这时只有一个“Codex（默认模型）”的入口。

### 圆桌具体是怎么调用的

```
codex exec --json --skip-git-repo-check --ephemeral -s read-only
           -c web_search=live|disabled
           -c developer_instructions=<圆桌的系统提示词>
           --disable shell_tool --disable unified_exec --disable apps --disable plugins ...
           --ignore-user-config --ignore-rules
           -c mcp_servers.<名字>.url=... 等      （只在圆桌里接了 MCP 时才有）
```

- 同样是没有改动过的官方程序，登录凭据由 Codex 自己管理。
- 关掉了它执行命令、用插件、操作浏览器/电脑、开子代理等能力，只留下对话、联网搜索（开了“联网”时）和画图。当前版本的 Codex 如果没有关闭命令行工具的开关，圆桌直接不启用它，而不是凑合着跑。
- 默认**不读你的 `~/.codex/config.toml`**（`--ignore-user-config`，登录不受影响）。原因是那里面可能配了 MCP 服务器，不拦的话聊天里的模型就能调用它们。副作用是你在 config.toml 里设的默认模型、推理强度之类对圆桌不生效——模型和强度在圆桌的界面里选。
- 如果你必须让它读 config.toml（比如自定义了模型供应商），在 `.env` 里设 `ROUNDTABLE_CODEX_USER_CONFIG=on`。这时圆桌会读出里面的 MCP 服务器名字，逐个加 `-c mcp_servers.<名字>.enabled=false` 关掉；其它设置照常生效，它们的后果由你自己判断。
- 想给模型接 MCP，请在圆桌的设置里接（见 [mcp.md](mcp.md)），不要指望 config.toml 里的那些。圆桌里接的服务器是通过 `-c` 参数单独交给 Codex 的，只指向本机网关。`ROUNDTABLE_CODEX_USER_CONFIG=on` 时，如果圆桌里某个服务器和 config.toml 里的重名，圆桌会报错而不是猜该用哪个。
- 每次运行用一个单独的临时目录，跑完删掉。
- Linux 上外面再套一层 bubblewrap 沙箱，细节见 [security.md](security.md)。

---

## CodeBuddy（腾讯）

腾讯的 CodeBuddy Code 命令行工具（`codebuddy`）。国内版里是混元、DeepSeek、GLM、Kimi、MiniMax 这些模型，用一个订阅就能都问到。WorkBuddy 的订阅和它是同一套账号、同一份额度，见下面“[规则](#腾讯codebuddy--workbuddy-订阅)”里引的原文。

### 安装

```bash
npm install -g @tencent-ai/codebuddy-code     # 需要 Node.js 18.20 以上
codebuddy --version
```

官方还有一个不用 Node 的安装脚本（文档里标着 Beta）：`curl -fsSL https://www.codebuddy.cn/cli/install.sh | bash`，装到 `~/.local/bin/codebuddy`，顺手在 `~/.profile` 里加一行 PATH。两种装法的 2.183.0 作者都测过，圆桌这边用起来没有区别。

圆桌会在 `PATH`、`~/.local/bin`、`~/.npm-global/bin` 里找 `codebuddy`；找不到就在 `.env` 里写 `ROUNDTABLE_CODEBUDDY_BIN=/完整/路径/codebuddy`，不想用写 `off`。没装的话界面上不会出现它。

### 登录

没有单独的登录命令，要进一次它的交互界面（用运行圆桌的那个用户）：

```bash
codebuddy
```

1. 第一次会问信不信任当前目录，回车。
2. 选登录方式：**国内版选 `Log in via Chinese Site`**，国际版选 `Log in via International Site`。
3. 服务器上没有浏览器时，它会打出一行 `Browser didn't open? Use the url below to sign in:` 和一条链接。把链接复制到自己电脑的浏览器里打开、登录，终端里会自己继续。
4. 登录成功后输入 `/exit` 退出。

登录状态存在 `~/.local/share/CodeBuddyExtension/` 下面。之后 `codebuddy -p` 自动沿用，并且按登录的站点决定用国内版还是国际版，不用另外设置。回到圆桌刷新页面，一两分钟内模型清单会换成你账号的那一份。（这一段是照它的文档和代码写的，作者没有账号，没法亲手验证，见下面的“测试范围”。）

也可以不登录、用 API 密钥：在 `.env` 里写

```
ROUNDTABLE_CLI_PASS_ENV=CODEBUDDY_API_KEY
CODEBUDDY_API_KEY=你的密钥
CODEBUDDY_INTERNET_ENVIRONMENT=internal     # 国内版；国际版不写这一行
```

官方文档原话：用 API 密钥时**必须**同时设 `CODEBUDDY_INTERNET_ENVIRONMENT`。（这条路作者没有用真的密钥测过。）

### 模型清单

圆桌不自己维护清单，每隔一阵问一次 `codebuddy`（用它 SDK 的控制请求，不发模型请求、不花额度），它说能选哪些就列哪些，一个不落。选模型的面板里每个模型后面带着它自己报的额度倍率（“额度 ×0.79”），倍率越高，同样的问答扣的额度越多。

思考强度：只有声明了档位的模型才给选（界面上点模型名），其余的用它固定的默认值。

### 圆桌具体是怎么调用的

```
codebuddy -p --model <模型 id>
          --input-format stream-json --output-format stream-json --include-partial-messages
          --system-prompt <圆桌的系统提示词>
          --tools "WebSearch,WebFetch"        （没开联网时是空的；这一轮带了图片时再加一个 ViewImage）
          --setting-sources none --strict-mcp-config --permission-mode dontAsk --no-session-persistence
          --allowedTools <上面那几个工具> <圆桌里勾选的 MCP 工具> ...   （一个名字一个参数）
          --mcp-config <临时文件>             （只在圆桌里接了 MCP 时才有）
环境变量：CODEBUDDY_DISABLE_AUTO_MEMORY=1  CODEBUDDY_DISABLE_SYSTEM_REMINDER_MD=1
          CODEBUDDY_SKIP_BUILTIN_MARKETPLACE=1  CODEBUDDY_DEFER_TOOL_LOADING=0  DISABLE_AUTOUPDATER=1
          CODEBUDDY_WEBFETCH_DISABLE_LOCAL_FALLBACK=1   （设了 ROUNDTABLE_CODEBUDDY_LOCAL_FETCH=on 就没有这一项）
```

- 同样是没有改动过的官方程序，登录凭据由它自己管理，圆桌不读。
- 它没有 Claude Code 那样一个“全部隔离”的总开关，隔离是上面这些参数和环境变量一项项拼出来的。每一项是干什么的、哪些是实测过的，见 [security.md](security.md) 的 CodeBuddy 一节。
- 抓网页：默认只走它的远程接口（腾讯那边的服务器替你抓），抓不到就是抓不到。想让它抓不到时改从你的服务器直接去抓，设 `ROUNDTABLE_CODEBUDDY_LOCAL_FETCH=on`，风险见 [security.md](security.md)。
- 图片：有的模型直接收图，有的要靠它的 `ViewImage` 工具让另一个模型看了再转述，由 `codebuddy` 自己决定，所以带图的时候圆桌把这个工具放开。
- PDF 和文档：一律抽成文字再给它（扫描件给前几页的图）。
- 每个回答下面除了字数，还会显示这一轮扣了多少额度（它报了才有）。
- 目前**不支持**用它画图。
- 内存：每个正在生成的回答是一个单独的进程，实测 350–400MB。内存小的机器把 `ROUNDTABLE_CONCURRENCY_CODEBUDDY` 调到 1 或 2。
- 它启动时要连一下自己那个站点的服务器；连不上的话每个回答开头会白等十秒左右（比如登的是国际版、而服务器在连不上国际站的网络里）。
- 上传过的图片，`codebuddy` 会在 `~/.codebuddy/blobs/` 里留一份副本。它自己有 `codebuddy cleanup` 命令清理过期数据。

### 测试范围

如实说明：写这一节的时候，作者手上**没有 CodeBuddy 的账号**。上面的调用方式、流式输出、联网工具、MCP、带图片、中途停止、出错提示，都是让 `codebuddy` 把模型请求发到一个**本机模拟的模型接口**上测的（它支持自定义模型地址），命令行那一层是真的，模型那一层是假的。所以这些**没有验证过**：真实账号登录之后的模型清单和登录状态显示、真实的联网搜索和抓网页、`ViewImage` 看图的效果、额度数字、额度用完和限频时的真实报错、国际版。遇到问题请提 issue，附上回答卡片上的报错。

---

## OpenRouter

1. 在 <https://openrouter.ai/> 注册、充值。
2. <https://openrouter.ai/settings/keys> 建一个密钥。**建议给这个密钥设一个额度上限**（Credit limit），万一泄露或者手滑，损失有顶。
3. `.env` 里填 `OPENROUTER_API_KEY=sk-or-v1-...`，重启。

模型清单从 OpenRouter 实时拉取（几百个），在选模型的面板里搜索，常用的可以收藏。每个回答的实际花费会显示在回答下面，设置的“用量”里有余额和近 30 天的统计。

要知道的几点：

- 你的消息和附件会发给 OpenRouter，再由它转给模型的提供方。各提供方的数据政策不一样，可以在 OpenRouter 的隐私设置里限制（比如不允许用于训练的提供方才可用）。
- 开了“联网”时，圆桌用的是 OpenRouter 的 web 插件，它**另外收费**，价格见 OpenRouter 的文档。
- 能画图的模型（输出里带图片的）会自动把图显示出来，按该模型的价格计费。

---

## 自动起标题

新对话的第一轮回答出来后，圆桌会让一个便宜的模型起个标题。**这会把你第一条消息的开头（最多 1500 字）和第一个回答的开头（最多 600 字）发给那个模型。**

`ROUNDTABLE_TITLE_MODEL`：

- `auto`（默认）：Claude Code 可用就用它的 `haiku`，否则不自动起标题（标题就是你消息的开头）。
- `off`：不起。
- `claude:<别名>`、`codebuddy:<模型 id>` 或 `or:<OpenRouter 模型 id>`：指定一个。

---

## 规则

下面是写这份文档时（**2026-11-10**）从各家公开页面上摘的原文。条款会变，摘录也可能断章取义——**请自己去读原文**，以原文为准。这一节只是帮你找到该读的地方，不是法律意见，也不是在说“这样用没问题”。

### 对谁都成立的底线

不管哪一家，下面这些事条款里都写明了不行，做了被封是意料之中的：

- **把账号给别人用。** 把圆桌的入口分享给家人、朋友、同事，让他们用你的订阅——这就是共享账号。圆桌设计成单人使用，登录名单里写多个邮箱时会警告，原因就在这里。
- **转卖、对外提供服务。** 不要把圆桌做成一个给别人用的站点，无论收不收钱。
- **改动官方程序、伪装客户端、绕过限额。** 圆桌不做这些事，你也不要改它去做。
- **刷量。** 订阅的额度是按“一个人正常使用”定的。挂着脚本自动灌问题、一次同时开很多个模型反复跑，都偏离了这个前提。`ROUNDTABLE_MAX_MODELS` 和并发上限就是为了让你不小心也跑不出离谱的量，别把它们调得很大。

### Anthropic（Claude 订阅 / Claude Code）

**Claude Code 文档 · Legal and compliance**　<https://code.claude.com/docs/en/legal-and-compliance>

> OAuth authentication is intended exclusively for purchasers of Claude Free, Pro, Max, Ultra, Team, and Enterprise subscription plans and is designed to support ordinary use of Claude Code and other native Anthropic applications.

> Developers building products or services that interact with Claude's capabilities, including those using the Agent SDK, should use API key authentication through Claude Console or a supported cloud provider.

> Anthropic does not permit third-party developers to offer Claude.ai login into their own applications, or to route requests through Free, Pro, Max, or Ultra plan credentials on behalf of their users. Moreover, developers may not collect, store, or intermediate Claude.ai credentials or session tokens

> Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription

> Advertised usage limits for Pro, Max, and Ultra plans assume ordinary, individual usage of Claude Code and the Agent SDK.

> The Claude Code binary must not be modified.

> Anthropic reserves the right to take measures to enforce these restrictions and may do so without prior notice.

**Consumer Terms of Service**（2025-10-08 生效）　<https://www.anthropic.com/legal/consumer-terms>

> You may not share your Account login information, Anthropic API key, or Account credentials with anyone else. You also may not make your Account available to anyone else.

禁止事项里有：

> Except when you are accessing our Services via an Anthropic API Key or where we otherwise explicitly permit it, to access the Services through automated or non-human means, whether through a bot, script, or otherwise.

**Usage Policy**（2026-11-12 生效的版本）　<https://www.anthropic.com/legal/aup>

> Resell, proxy, or otherwise provide access to Claude through unauthorized means, including services that route requests through consumer subscriptions or misrepresent the product or client being used

**帮助中心 · Use the Claude Agent SDK with your Claude plan**　<https://support.claude.com/en/articles/15036540>

2026-10-07 的更新：

> Nothing changes for your plan: when you’re signed in with your Claude plan, the Claude Agent SDK and `claude -p` still draw from your subscription limits.

2026-06-15 的更新：

> We’ve paused the previously-announced changes to Claude Agent SDK usage. For now, nothing has changed: Claude Agent SDK, `claude -p`, and third-party app usage still draw from your subscription limits.

**Claude Code 文档 · Headless / Authentication**　<https://code.claude.com/docs/en/headless>　<https://code.claude.com/docs/en/authentication>

> `--bare` is the recommended mode for scripted and SDK calls, and will become the default for `-p` in a future release.

> In bare mode, Claude Code never reads OAuth credentials or the system keychain.

**圆桌和这些条文的关系，以及没说清的地方：**

- 圆桌做的事是：在你自己的机器上，运行没有改动过的 `claude` 程序，用你自己登录的账号，回答你自己的问题。它不提供“用 Claude 账号登录”的功能，不接触凭据，不替别人转发请求。条文里明确禁止的几件事（第三方收集凭据、替用户走订阅、改程序），它没有做。
- **但是**，“在一个第三方的开源界面后面调用 `claude -p`”算不算条文说的 “ordinary use of Claude Code”，还是算“开发者做的产品或服务”（那就应该用 API 密钥），没有任何一页给出明确答案。
- Consumer Terms 禁止“自动化或非人工方式”访问，除非用 API 密钥或者“另有明确许可”。官方文档里写了 `-p` 和 `setup-token` 的用法，帮助中心也说订阅下 `claude -p` 的用量算在订阅额度里，但没有哪一页直接说“这就是那个明确许可”。
- “ordinary, individual usage” 没有定义。
- 帮助中心的措辞是 “For now”。文档也预告了 `-p` 将来默认 `--bare`，而 bare 模式不读订阅登录——到那时用订阅登录跑 `-p` 可能就不再可行，这个后端只能改用 API 密钥。
- 除了账号持有人自己以外的任何人使用，都落在“共享账号”条款上，这一点没有歧义。

### OpenAI（ChatGPT 订阅 / Codex）

**Codex 文档 · Authentication**　<https://learn.chatgpt.com/docs/auth>

Codex 支持两种登录：

> Sign in with ChatGPT for subscription access

> Sign in with an API key for usage-based access

> Use API key authentication for programmatic Codex CLI workflows, such as CI/CD jobs. Don’t expose Codex execution in untrusted or public environments.

> Treat `~/.codex/auth.json` like a password: it contains access tokens.

**Codex 文档 · Non-interactive mode**　<https://learn.chatgpt.com/docs/non-interactive-mode>

> `codex exec` reuses saved CLI authentication by default.

> API keys are the right default for automation because they are simpler to provision and rotate. Use this path only if you specifically need to run as your Codex account.

（后一句的 “this path” 指的是在自动化里用 ChatGPT 登录。）

**Codex 文档 · Pricing**　<https://learn.chatgpt.com/docs/pricing>

> Local messages and cloud chats share your plan’s usage allowance. Weekly limits may also apply.

> All users may also run extra local chats using an API key, with usage charged at standard API rates.

**Terms of Use**（2026-01-01 生效）　<https://openai.com/policies/terms-of-use/>

> You may not share your account credentials or make your account available to anyone else

禁止事项里有：

> Modify, copy, lease, sell or distribute any of our Services.

> Automatically or programmatically extract data or Output

> Interfere with or disrupt our Services, including circumvent any rate limits or restrictions or bypass any protective measures

**没说清的地方：**

- Terms of Use 通篇没有提 Codex，也没有说官方命令行工具的非交互模式是否属于“以程序方式提取输出”。
- Codex 文档推荐自动化场景用 API 密钥，但没有禁止在 ChatGPT 登录下使用 `codex exec`，反而写了它默认沿用已保存的登录。
- 第三方界面包一层官方 CLI 的做法，没有任何一页提到。
- “fair use limits” 没有定义。
- “不要在不可信或公开的环境里暴露 Codex 的执行能力”——圆桌必须放在登录后面、只给你自己用，原因之一就是这句。

### 腾讯（CodeBuddy / WorkBuddy 订阅）

写这一小节的日期是 2026-11-11。

**WorkBuddy 定价页**　<https://www.codebuddy.cn/docs/workbuddy/Pricing>

> WorkBuddy 和 CodeBuddy 同一账号积分共享，无需分别订阅。

同一页的套餐对比表里，“CLI 访问支持”一行：免费的体验版是“—”，付费的几档是“✓”。也就是说**免费档不含命令行**。

**腾讯云 Buddy AI 个人版订阅服务协议**　<https://rule.tencent.com/rule/202606290002>

> 该权益支持WorkBuddy（含PC/APP/小程序）与CodeBuddy（含PC/CLI）多端互通共享、统一计费。

> 您购买本服务后仅享有本服务使用权，且仅限您自身使用，未经腾讯云允许，您不得将享有本服务的账号提供给授权范围以外的他人使用，包括不得以转让、出租、借用、分享、出售等方式提供给他人

同一条列出的禁止行为里有：

> 以超出订阅套餐许可范围的商业目的为自己或他人开通本服务，例如将本服务用于对外提供商业化AI能力

> 通过机器人软件、蜘蛛软件、爬虫软件、刷屏软件、自动化脚本等程序、软件方式……为自己或他人开通本服务或滥用积分

后果写的是限制功能、清除权益、封禁账号，“并不退还您已支付的费用”。

**腾讯云 WorkBuddy 软件许可及服务协议**　<https://rule.tencent.com/rule/202603180001>

> “使用”包括您直接使用本软件及服务，或通过您授权、配置、接入的第三方软件、系统、工具、插件、自动化程序、智能体等方式调用或操作本软件及服务的行为。

> 您可以为非商业目的在一个或多个设备上使用本软件及服务。

> 您的账号仅限于您个人使用……您不得赠与、借用、出租、转让、售卖或者以其他方式许可他人使用账号。

**CodeBuddy Code 文档 · 身份认证**　<https://www.codebuddy.ai/docs/cli/iam>

文档写了三种给程序用的认证方式（`CODEBUDDY_AUTH_TOKEN`、`apiKeyHelper`、`CODEBUDDY_API_KEY`），并说明 API 密钥在非交互模式（`-p`）下总是生效。

**没说清的地方：**

- 协议把“通过第三方工具、自动化程序调用”算进了“使用”的定义里，没有禁止；但也没有哪一条专门讲 `codebuddy -p` 配订阅登录、外面再包一层界面这种用法。
- “滥用积分”没有定义。
- 账号只能本人用，这一点三处原文都写了，没有歧义：把圆桌的入口给别人，就是把账号给别人用。
- 许可范围是“非商业目的”。

### OpenRouter

**Terms of Service**（2026-08-31 更新）　<https://openrouter.ai/terms>

> You are responsible for maintaining the confidentiality and security of all API keys, tokens, passwords, and other credentials

> You are responsible for all activity and charges under its account or API Credentials, whether or not authorized by you

> By accessing or using any Model through the Service, you agree… to comply with the applicable terms for each Model

禁止事项里有以转售模型 API 访问或开发竞争服务为目的使用、以及转让访问权。

API 密钥本来就是给程序用的，个人自己部署一个界面来调用，条款里没有专门的限制。要注意的是钱：密钥泄露产生的费用算你的，所以给密钥设额度上限，并且不要把圆桌暴露在没有登录保护的地方。另外，通过 OpenRouter 用某个模型，还要遵守那个模型提供方自己的使用政策。

### 如果你想尽量稳妥

1. 每个后端都用 API 密钥，按量付费，并在各家后台设消费上限。
2. 只给自己用：登录名单里只写一个邮箱，不分享入口，不分享密码。
3. 不要改圆桌去绕任何限制；被限流了就等，或者换 API 密钥。
4. 隔一段时间重读上面的链接。尤其是 Anthropic 的 Legal and compliance 页面和帮助中心那篇文章，这两处近一年改过不止一次。
5. 条款变了、你觉得不再合适了，就把对应的后端关掉：`ROUNDTABLE_CLAUDE_BIN=off` / `ROUNDTABLE_CODEX_BIN=off` / `ROUNDTABLE_CODEBUDDY_BIN=off`。

### 免责

本项目与 Anthropic、OpenAI、腾讯、OpenRouter、Google 没有任何关系，没有得到它们的认可或授权。Claude、Claude Code、ChatGPT、Codex、CodeBuddy、WorkBuddy、OpenRouter 等名称属于各自的权利人。

本项目按“原样”提供，不作任何保证。是否使用、用哪种登录方式、是否符合你与各服务商之间的协议，由你自己判断并承担全部后果。作者和贡献者不对任何账号限制、封禁、费用、数据丢失或其他损失负责。
