"""外部登录（ROUNDTABLE_AUTH=external）的示例文件。复制到仓库外面（比如 /etc/roundtable/auth.py）再改，
然后在 .env 里设 ROUNDTABLE_AUTH_PLUGIN=/etc/roundtable/auth.py。为什么会有这个东西、它该做什么，见 docs/login.md。

这个示例对应最常见的一种部署：前面的反向代理已经做了登录（oauth2-proxy、Authelia、Caddy 的 forward_auth……），
它把登录的人写在一个请求头里转给后面的应用。光认这个头是不够的——谁能直接连到应用的端口，谁就能自己写这个头——
所以这里还要求代理带上一个只有它知道的密钥：

  .env 里：      PROXY_SECRET=一长串随机字符        （.env 里的每一项这个文件都能用 os.environ 读到）
  代理的配置里：  转发时加请求头  X-Proxy-Secret: 同一串字符，并且**删掉**客户端自己带来的 X-Proxy-Secret 和 X-Forwarded-Email

你的登录网关如果有别的更可靠的核对办法（查它的会话库、验它签名的令牌），照那个来写更好。
"""
import hmac
import os

SECRET = os.environ.get("PROXY_SECRET", "")
USER_HEADER = "x-forwarded-email"          # 请求头的名字一律小写

# 可选：没登录的人直接打开页面时送去哪。一般到不了这一步（代理先拦了）。
# LOGIN_URL = "https://sso.example.com/login"


def authenticate(request: dict) -> str | None:
    """request = {"site": "chat" 或 "term", "headers": {...}, "cookies": {...}, "client": 对端地址}
    返回用户标识 = 放行；返回 None = 没登录；抛 PermissionError("原因") = 登录了但不许进。
    每个请求都会调一次，别在这里面发网络请求。"""
    h = request["headers"]
    if len(SECRET) < 24 or not hmac.compare_digest(h.get("x-proxy-secret", ""), SECRET):
        return None                        # 不是经过代理来的
    user = h.get(USER_HEADER, "").strip().lower()
    if not user:
        return None
    # 想让终端比对话更严，可以在这里看 request["site"]：
    # if request["site"] == "term" and user != "me@example.com":
    #     raise PermissionError("这个账号不能进终端")
    return user
