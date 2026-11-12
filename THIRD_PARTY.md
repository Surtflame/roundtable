# 第三方组件

圆桌自己的代码按 MIT 许可发布（见 [LICENSE](LICENSE)）。下面是它用到的别人的东西。

## 随仓库一起分发的前端库（`static/vendor/`）

这些文件是各项目官方发布的原样文件，内容没有改动（两个代码高亮主题的样式文件改了文件名），校验和在 `static/vendor/SHA256SUMS`，各自的许可证全文在 `static/vendor/licenses/`。

| 文件 | 项目 | 版本 | 许可 |
|---|---|---|---|
| `highlight.min.js`、`hljs-light.css`、`hljs-dark.css` | [highlight.js](https://github.com/highlightjs/highlight.js)（样式是 GitHub / GitHub Dark 主题） | 11.12.0 | BSD-3-Clause |
| `markdown-it.min.js` | [markdown-it](https://github.com/markdown-it/markdown-it) | 14.3.3 | MIT |
| `texmath.js` | [markdown-it-texmath](https://github.com/goessner/markdown-it-texmath) | — | MIT |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.17 | Apache-2.0 或 MPL-2.0（这里按 Apache-2.0 使用） |
| `katex.min.js`、`katex.min.css` | [KaTeX](https://github.com/KaTeX/KaTeX) | 0.16.47 | MIT |
| `fonts/KaTeX_*.woff2` | KaTeX 字体 | 0.16.47 | SIL Open Font License 1.1 |

网页终端（可选，`static_term/vendor/xterm/`）用的终端模拟器，同样是官方发布的原样文件，校验和在同目录的 `SHA256SUMS`，许可证全文是同目录的 `LICENSE`：

| 文件 | 项目 | 版本 | 许可 |
|---|---|---|---|
| `xterm.js`、`xterm.css` | [xterm.js](https://github.com/xtermjs/xterm.js)（`@xterm/xterm`） | 6.0.0 | MIT |
| `addon-fit.js`、`addon-unicode11.js`、`addon-web-links.js`、`addon-clipboard.js` | xterm.js 的官方插件（`@xterm/addon-*`，和 6.0 配套发布的版本）。`addon-clipboard.js` 里打包了 [js-base64](https://github.com/dankogai/js-base64) | — | MIT（js-base64 是 BSD-3-Clause） |

## 安装时从 PyPI 下载的 Python 依赖

不随仓库分发，由 `pip` 安装。许可信息取自各个包自己声明的元数据，以它们的官方说明为准。

| 包 | 用途 | 许可 |
|---|---|---|
| fastapi | Web 框架 | MIT |
| uvicorn | ASGI 服务器 | BSD-3-Clause |
| httpx | HTTP 客户端 | BSD-3-Clause |
| python-multipart | 解析上传的表单 | Apache-2.0 |
| pypdfium2 | 读 PDF（内含 PDFium） | BSD-3-Clause / Apache-2.0，外加 PDFium 及其依赖各自的许可 |
| pillow | 处理图片 | MIT-CMU |
| openpyxl | 读 Excel | MIT |
| python-docx | 读 Word | MIT |
| python-pptx | 读 PowerPoint | MIT |
| websockets | 网页终端的 WebSocket | BSD-3-Clause |
| segno | 网页终端绑定验证器时的二维码 | BSD-3-Clause |

它们各自还会带进来一些间接依赖（starlette、pydantic、lxml 等），完整清单见 `requirements.lock`。

**可选依赖 pillow-heif**（读 HEIC 图片）没有列入默认依赖：它在 PyPI 上的二进制安装包里带了 x265，整体按 GPLv2 发布。需要的话自己 `pip install pillow-heif`，圆桌检测到就会用。

## 不随圆桌分发、需要你自己安装的程序

- **Claude Code**（Anthropic）和 **Codex CLI**（OpenAI）：圆桌只是以子进程的方式调用你机器上装好的官方程序，不包含、不修改它们。它们的使用受各自的条款约束，见 [docs/backends.md](docs/backends.md)。
- **bubblewrap**（可选，LGPL-2.0-or-later）：以独立程序的方式调用。

圆桌和 Anthropic、OpenAI、OpenRouter、Google 没有任何关联，也没有得到它们的认可。这些名字只是用来说明圆桌能接哪些服务。
