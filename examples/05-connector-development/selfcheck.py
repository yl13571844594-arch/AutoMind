"""端到端自测：给示例连接器起一个假的工单系统，真发一次 HTTP，看工具跑通没有。

## 它验证什么

不是"文件能 import"这种半程验证，而是把整条链路走完：

1. 把 ``ticket_status.py`` 复制进一个临时连接器目录；
2. 用标准库起一个本地 HTTP 服务当"客户工单系统"（返回一段真实结构的 JSON）；
3. 走**平台自己的加载器** ``load_connectors()`` 把它加载进 ``ToolRegistry``；
4. 通过注册表 ``dispatch()`` 真的调一次（和模型调用走的是同一条路）；
5. 断言拿回来的 ``ToolResult`` 里是那段 JSON 的数据。

一旦这五步都过，"连接器能被平台加载、能被模型调用、能拿到内部 API 的数据"
就都被证明过了 —— 而这三件事恰恰是交付时最容易出问题的地方。

用法::

    python examples/05-connector-development/selfcheck.py

退出码 0 = 通过；1 = 失败（失败原因会打出来）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CONNECTOR = HERE / "ticket_status.py"

#: 假工单系统返回的"一条工单" —— 字段名照着真实系统的常见形状编。
FAKE_TICKET = {
    "id": "TK-1024",
    "title": "打印机连不上网络",
    "status": "open",
    "assignee": "张工",
    "updated_at": "2026-01-01T09:30:00+08:00",
    "url": "https://tickets.internal.example.com/TK-1024",
}


class _FakeTicketApi(BaseHTTPRequestHandler):
    """一个只会返回上面那条工单的 HTTP 服务（够用就够，不引任何依赖）。"""

    def do_GET(self) -> None:                             # noqa: N802 - BaseHTTPRequestHandler 约定
        # 连接器把 TICKET_API_BASE 当作 API 根（…/api/v1），路径由它自己拼，
        # 所以这里匹配的是含前缀的完整路径。
        if self.path.startswith("/api/v1/tickets/TK-1024"):
            body = json.dumps(FAKE_TICKET).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404, "no such ticket")

    def log_message(self, *args) -> None:
        """默认实现会往 stderr 打访问日志 —— 自测输出里不需要它。"""


def _start_fake_api() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _FakeTicketApi)   # 0 = 让系统挑个空闲端口
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/api/v1"


async def _run() -> int:
    if not CONNECTOR.is_file():
        print(f"× 找不到示例连接器：{CONNECTOR}")
        return 1

    # 隔离：临时连接器目录 + 指向本地假服务的配置，绝不碰用户自己的
    # ~/.automind/connectors 或真实工单系统。
    workdir = REPO_ROOT / f".connector-selfcheck-{os.getpid()}"
    connectors_dir = workdir / "connectors"
    connectors_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CONNECTOR, connectors_dir / CONNECTOR.name)

    server, base_url = _start_fake_api()
    os.environ["AUTOMIND_CONNECTORS_DIR"] = str(connectors_dir)
    os.environ["TICKET_API_BASE"] = base_url
    os.environ["TICKET_API_TOKEN"] = "selfcheck-fake-token"   # 真令牌是 ASCII，别用中文试
    if str(REPO_ROOT) not in sys.path:                    # 未 pip install 时也能跑
        sys.path.insert(0, str(REPO_ROOT))

    failures: list[str] = []
    try:
        from automind.tools.base import ToolRegistry
        from automind.tools.connectors import (
            load_connectors,
            load_failures,
            reload_connectors,
        )

        registry = ToolRegistry()

        # ① 加载
        loaded = load_connectors(registry)
        print(f"1) 加载连接器 → {loaded or '（一个都没有）'}")
        if loaded != ["ticket_status"]:
            failures.append(f"该加载出 ticket_status，实际是 {loaded}；"
                            f"加载失败账目：{load_failures()}")

        # ② 真的调一次（和模型走同一条 dispatch 路径）
        result = await registry.dispatch("ticket_status", action="get", ticket_id="TK-1024")
        print(f"2) 调用 ticket_status → success={result.success} output={result.output}")
        if not result.success:
            failures.append(f"调用失败：{result.error}")
        else:
            if result.output.get("ticket_id") != "TK-1024":
                failures.append(f"工单号不对：{result.output.get('ticket_id')!r}")
            if result.output.get("state") != "open":
                failures.append(f"状态没解析出来：{result.output.get('state')!r}")

        # ③ 模型能看见的参数 schema（少了这一步，工具等于没注册）
        schema = registry.get("ticket_status").to_openai_schema()
        print(f"3) 下发给模型的 schema → name={schema['name']} "
              f"params={sorted(schema['parameters']['properties'])}")
        if "ticket_id" not in schema["parameters"]["properties"]:
            failures.append("schema 里没有 ticket_id，模型没法传参")

        # ④ reload 之后仍然可用（构建交付流程里必点的一次）
        out = reload_connectors(registry)
        print(f"4) 重载 → {out}")
        if out["loaded"] != ["ticket_status"] or out["failed"]:
            failures.append(f"重载结果不对：{out}")
    finally:
        server.shutdown()
        shutil.rmtree(workdir, ignore_errors=True)

    if failures:
        print("\n× 自测没过：")
        for f in failures:
            print(f"  · {f}")
        return 1
    print("\n√ 全部通过：连接器能被加载、能被调用、能拿到内部 API 的数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
