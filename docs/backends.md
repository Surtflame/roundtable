# 模型后端：怎么配，以及各家的规则

圆桌自己不带任何模型，也不带任何账号。它调用你**自己装的、自己登录的**三样东西：

| 后端 | 圆桌怎么调用 | 钱怎么算 |
| --- | --- | --- |
| Claude Code | 在服务器上运行官方的 `claude -p` 命令 | 你的 Claude 订阅额度，或者 Anthropic API 密钥按量 |
| Codex | 在服务器上运行官方的 `codex exec` 命令 | 你的 ChatGPT 订阅额度，或者 OpenAI API 密钥按量 |
| OpenRouter | 直接发 HTTPS 请求 | OpenRouter 余额，按量 |

至少配一个，配几个都行。没配的后端在界面里不出现；没配成功的，原因写在左下角设置的“用量”里，也可以运行 `python -m roundtable check` 看。

> **先读这一段。**
> 用订阅账号（而不是 API 密钥）驱动这样一个应用，是否符合 Anthropic 和 OpenAI 的条款，**两家都没有给出明确的说法**，本文后半部分的“[规则](#规则)”把能找到的原文和没说清的地方都列出来了。
> 用不用、怎么用，由部署的人自己判断、自己负责。因为使用本项目导致账号被限流、被暂停、被封禁、被额外扣费或者任何其他后果，本项目的作者和贡献者不承担任何责任（另见 [LICENSE](../LICENSE) 的免责条款）。
> 如果你不想碰任何灰色地带：三个后端都用 **API 密钥**，按量付费。这是各家文档明确写了给程序用的方式。

---

## 一个前提：用哪个系统用户

`claude` 和 `codex` 的登录状态存在**系统用户的家目录**里（`~/.claude`、`~/.codex`）。圆桌以哪个用户运行，就用哪个用户的登录状态。所以：

- 下面的登录命令，要用**运行圆桌的那个用户**去执行。用 systemd 的话就是服务文件里的 `User=`。
- 不要用 root 运行。
- 建议给圆桌单独建一个系统用户，或者至少别用一个平时放了很多别的东西的账号，理由见 [security.md](security.md)。

---

## Claude Code

### 安装

官方安装说明：<https://code.claude.com/docs/en/setup>。常见的装法：

```bash
npm install -g @anthropic-ai/claude-code      # 需要 Node.js 18+
claude --version
```

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

默认列出 Claude Code 自带的几个模型别名，别名具体指向哪个版本由你装的 Claude Code 决定；你的套餐不包含的模型选了会报错。想自己定清单：

```bash
ROUNDTABLE_CLAUDE_MODELS=opus=Claude Opus,sonnet=Claude Sonnet,haiku=Claude Haiku
```

等号左边是别名或完整的模型 id，右边是显示名。

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
npm install -g @openai/codex
codex --version
```

装在了 PATH 以外的地方，写 `ROUNDTABLE_CODEX_BIN=/完整/路径/codex`；不想用就写 `ROUNDTABLE_CODEX_BIN=off`。

Linux 上建议同时装 bubblewrap，圆桌会把 Codex 关在里面运行（见 [security.md](security.md)）：

```bash
sudo apt install bubblewrap        # Debian / Ubuntu
sudo dnf install bubblewrap        # Fedora
```

没有 bubblewrap（比如 macOS）也能用，只是少一层隔离，启动日志和设置页会提醒。想强制“没有沙箱就不启用 Codex”，设 `ROUNDTABLE_CODEX_SANDBOX=bwrap`。

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

读的是 Codex 自己缓存的模型清单（`~/.codex/models_cache.json`），你的账号能用哪些就列哪些。刚装好、还没运行过时只有一个“Codex（默认模型）”的入口，用过一次之后清单就出来了。

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
- `claude:<别名>` 或 `or:<OpenRouter 模型 id>`：指定一个。

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

### OpenRouter

**Terms of Service**（2026-08-31 更新）　<https://openrouter.ai/terms>

> You are responsible for maintaining the confidentiality and security of all API keys, tokens, passwords, and other credentials

> You are responsible for all activity and charges under its account or API Credentials, whether or not authorized by you

> By accessing or using any Model through the Service, you agree… to comply with the applicable terms for each Model

禁止事项里有以转售模型 API 访问或开发竞争服务为目的使用、以及转让访问权。

API 密钥本来就是给程序用的，个人自己部署一个界面来调用，条款里没有专门的限制。要注意的是钱：密钥泄露产生的费用算你的，所以给密钥设额度上限，并且不要把圆桌暴露在没有登录保护的地方。另外，通过 OpenRouter 用某个模型，还要遵守那个模型提供方自己的使用政策。

### 如果你想尽量稳妥

1. 三个后端都用 API 密钥，按量付费，并在各家后台设消费上限。
2. 只给自己用：登录名单里只写一个邮箱，不分享入口，不分享密码。
3. 不要改圆桌去绕任何限制；被限流了就等，或者换 API 密钥。
4. 隔一段时间重读上面的链接。尤其是 Anthropic 的 Legal and compliance 页面和帮助中心那篇文章，这两处近一年改过不止一次。
5. 条款变了、你觉得不再合适了，就把对应的后端关掉：`ROUNDTABLE_CLAUDE_BIN=off` / `ROUNDTABLE_CODEX_BIN=off`。

### 免责

本项目与 Anthropic、OpenAI、OpenRouter、Google 没有任何关系，没有得到它们的认可或授权。Claude、Claude Code、ChatGPT、Codex、OpenRouter 等名称属于各自的权利人。

本项目按“原样”提供，不作任何保证。是否使用、用哪种登录方式、是否符合你与各服务商之间的协议，由你自己判断并承担全部后果。作者和贡献者不对任何账号限制、封禁、费用、数据丢失或其他损失负责。
