"""HTTP 请求来源加固 —— 按 ``Host`` 头判定"这是本机在访问自己"。

## 修的是什么：DNS 重绑定

服务默认绑回环（``127.0.0.1``），此前据此认为"回环 == 本机用户"，于是把
好几项信任直接挂在了**客户端 IP** 上：

* 管理动作（改 ``api_base`` / 加 MCP / 加载插件 = 任意代码 / 触发更新）；
* 目录浏览（枚举磁盘）；
* ``/api/integrations/continue``（会吐明文访问令牌）。

这个推断有一个反例：**DNS 重绑定**。攻击者让自己的域名解析先指向自己的
服务器（页面正常加载、脚本跑起来，页面 Origin = ``http://evil.example``），
随后把该域名的 TTL=0 记录改成 ``127.0.0.1``。浏览器此时再请求
``http://evil.example:8765/api/...``，**TCP 连的是本机、Host 头却仍是
``evil.example``**（浏览器只管把域名换成 IP，不改 Host）。于是
``request.client.host`` 是回环，上面每一项信任全部成立 ——

* 管理动作没有 CORS 概念（跨站 POST 是"简单请求"，浏览器照发不误），
  页面读不到响应不影响它**已经生效**：``api_base`` 一改，此后每次对话的
  提示词与 API Key 都从攻击者的地址过；
* 目录浏览、令牌端点这类**读**接口一旦拿到 CORS 放行（本机来源正则
  ``_LOCAL_ORIGIN_RE`` 匹配的是 ``Origin``，而 Origin 是攻击者的域名，
  不匹配 → 读不到）虽被同源策略挡住，但把它们与写接口一起守住更省心。

## 判定方式

回环部署下，**浏览器取回的 Host 头必然是 ``localhost`` / ``127.0.0.1`` /
``[::1]``（带端口）**；重绑定请求的 Host 则是攻击者的域名。因此：

* 回环字面量 → 放行；
* 显式列进 ``AUTOMIND_TRUSTED_HOSTS``（逗号分隔，支持 ``*.example.com``
  与 ``*``）→ 放行，给反向代理 / Tailscale / ngrok 这类"Host 是外部名字、
  但确实是我自己发布出去"的用法留门；
* 其它 → 403，且错误信息里写明怎么放行（不给用户一个没有出路的 403）。

**为什么是 403 而不是静默放行**：Host 不属于自己却连得上本机服务，只有两种
可能 —— 重绑定攻击，或者用户把服务发布出去了却还没登记名字。两种都需要用户
当场知道，而不是让他在不知情的情况下把管理台交出去。

只挡带 Host 的请求：HTTP/1.0 客户端与部分测试客户端可能不带 Host，此时没有
可判据，交回给原有的 IP 判定（``_is_local_request``），保持既有行为。
"""

from __future__ import annotations

import os
import re

_DEFAULT_PORT = 8765

#: 服务绑定的默认主机（server 的 ``--host`` 与桌面版都用它）
DEFAULT_HOST = "127.0.0.1"

#: ``--host`` / ``AUTOMIND_HOST`` 的空值与等价通配写法 —— 都表示"全网卡监听"
_BIND_ALL = {"", "0.0.0.0", "::", "*"}


def bind_host() -> str:
    """当前服务绑定地址（``AUTOMIND_HOST`` → 默认回环）。"""
    return (os.environ.get("AUTOMIND_HOST") or "").strip() or DEFAULT_HOST


def binds_to_loopback() -> bool:
    """服务是否只监听回环。绑到全网卡时 Host 判定不再适用（见 :func:`host_allowed`）。"""
    return bind_host().lower() not in _BIND_ALL


#: 回环的几种合法写法（不含端口，比较前会先剥端口、去尾点、转小写）
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1",
                   "localhost.localdomain"}

#: 测试客户端的占位 Host（Starlette ``TestClient`` 默认发 ``Host: testserver``）。
#:
#: 为什么必须单独认它：**它不是可达的地址**，因此拿它当判据没有安全成本 ——
#: 重绑定攻击要成立，Host 必须是攻击者**真正拥有并能解析**的域名，而
#: ``testserver`` 在本机 hosts 里解析到回环、在别处根本不解析（RFC 2606 保留），
#: 攻击者用它连不到任何东西。
#:
#: 不认它的代价则是实打实的：全套服务端用例都经 TestClient 发请求，
#: 要么这里放行，要么去改上百个用例的构造方式 —— 后者只会让这套护栏
#: 在下一个新用例里被绕过去。宁可在此显式登记一个不可达名字。
_TEST_CLIENT_HOSTS = {"testserver"}

#: 回环网段：``127.0.0.0/8`` 全是本机，浏览器用 ``127.0.0.1``，代理可能用别的
_LOOPBACK_RE = re.compile(r"^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


def split_host_port(netloc: str) -> str:
    """取 Host 头里的主机名：剥掉端口、去掉尾部点、小写。

    ``[::1]:8765`` → ``::1``；``127.0.0.1:8765`` → ``127.0.0.1``；
    ``LocalHost.`` → ``localhost``。
    """
    loc = (netloc or "").strip().strip("/").split("/", 1)[0]
    if not loc:
        return ""
    # userinfo（``http://user@host/``）—— 解析失败时也要能剥掉，否则会把
    # ``127.0.0.1@evil.com`` 当成回环（这正是前缀匹配类判定的经典翻车点）
    if "@" in loc:
        loc = loc.rsplit("@", 1)[1]
    if loc.startswith("["):
        end = loc.find("]")
        host = loc[1:end] if end > 0 else loc[1:]
    else:
        host = loc.rsplit(":", 1)[0] if ":" in loc else loc
    return host.rstrip(".").strip().lower()


def trusted_hosts() -> list[str]:
    """``AUTOMIND_TRUSTED_HOSTS`` 里登记的主机名（小写，可含 ``*.`` 通配）。

    登记项写 ``host:port`` 也认 —— 直接取主机名部分，免得用户按"访问地址"
    的习惯写法填了端口，结果被自己填的这行字**静默忽略**（这条护栏最不该
    有的行为就是"看起来配了、其实没生效"），也不至于让同一条规则
    在带端口/不带端口的请求上表现不一致。
    """
    raw = os.environ.get("AUTOMIND_TRUSTED_HOSTS", "") or ""
    out: list[str] = []
    for item in raw.split(","):
        host = split_host_port(item)
        if host and host not in out:      # 空项（如 `a,,b`）与非主机名一律丢弃
            out.append(host)
    return out


def _matches_extra(host: str, pattern: str) -> bool:
    """登记项匹配：``*`` 全放行，``*.example.com`` 匹配子域，其余精确匹配。"""
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) or host == pattern[2:]
    return host == pattern


def host_allowed(host_header: str) -> tuple[bool, str]:
    """该 Host 头是否允许访问本服务。返回 ``(允许, 拒绝原因)``。"""
    host = split_host_port(host_header)
    if not host:
        # 没有可判据（HTTP/1.0 或测试客户端）：交回调用方的既有判断
        return True, ""
    if not binds_to_loopback():
        # 绑全网卡是显式选择（内网共享/容器）；此时 Host 必然是外部名字，
        # 按 Host 拦会把正常用法全拦掉 —— 由 auth_token 负责这一场景的准入。
        return True, ""
    if host in _LOOPBACK_HOSTS or _LOOPBACK_RE.match(host):
        return True, ""
    if host in _TEST_CLIENT_HOSTS:
        return True, ""
    pattern_hit = next((p for p in trusted_hosts() if _matches_extra(host, p)), "")
    if pattern_hit:
        return True, ""
    return False, (
        f"拒绝访问：请求的 Host 是 “{host}”，而本服务只服务本机地址。"
        "这通常是 DNS 重绑定攻击的特征（把外部域名解析到 127.0.0.1 来借用"
        "「本机 = 可信」的判定）。请改用 http://localhost 或 http://127.0.0.1 "
        "访问。若确实要通过反向代理 / 内网域名访问，请把它加进环境变量 "
        f"AUTOMIND_TRUSTED_HOSTS（逗号分隔，当前值："
        f"{','.join(trusted_hosts()) or '（空）'}）。")


def local_hosts() -> list[str]:
    """本机访问可用的地址（供启动日志与手册提示用）。"""
    return ["localhost", "127.0.0.1", "[::1]"]


__all__ = [
    "DEFAULT_HOST",
    "bind_host",
    "binds_to_loopback",
    "host_allowed",
    "local_hosts",
    "split_host_port",
    "trusted_hosts",
]
