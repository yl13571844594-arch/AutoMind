"""进程内在线成员注册表（v1.11.15 团队「在线感知」）。

为什么是进程内而不是存库：
    在线状态是**瞬时事实** —— 进程重启后还留着"某某在线"是纯谎报。持久化的
    只有昵称（``.automind/team_members.json``，见 :mod:`automind.core.team`），
    在线表在启动时天然为空，符合"重启后未连接即离线"的直觉。

聚合口径：
    **按 sid 聚合**，不是按 ws 连接。同一个浏览器开三个标签页（三条 ws）应当
    只显示为一个人；一个标签页切会话/工作区时通过 :func:`bind` 更新 sid。
    前端传入的是工作区级 sid（同一浏览器的标签页一致），因此聚合结果稳定。

线程安全：
    Express/FastAPI 的事件循环是单线程，但 TestClient、线程池与将来的多 worker
    形态都可能并发进出，故所有入口统一加可重入锁。数据量是"在线连接数"级别，
    锁竞争可以忽略。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from typing import Any

#: 在线连接登记表：client_id → {client_id, sid, name, named, online, last_seen, joined}
_clients: dict[str, dict] = {}
#: sid → 显式昵称（用户保存过的 / 本进程内改名过的）；重连后据此恢复显示名
_known_names: dict[str, str] = {}
_lock = threading.RLock()

#: 防御性上限：异常客户端疯狂重连也不该把内存撑爆。超出时丢最早活跃的。
MAX_CLIENTS = 500

#: 自动昵称前缀（前端用它判断"这是系统给的临时名"也无妨，但语义上仅作图省事）
DEFAULT_NAME_PREFIX = "成员-"

_CLOCK_FMT = "%Y-%m-%d %H:%M:%S"


def _now() -> str:
    return time.strftime(_CLOCK_FMT)


def _clean(value: Any, limit: int = 128) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def default_name(sid: Any = "", client_id: Any = "") -> str:
    """缺省昵称：``成员-<sid 或 client_id 后 4 位>``。"""
    key = _clean(sid) or _clean(client_id)
    if key in ("", "default"):
        key = _clean(client_id)
    return f"{DEFAULT_NAME_PREFIX}{key[-4:] if key else '0000'}"


def _name_for(sid: str, client_id: str, name: Any = "") -> tuple[str, bool]:
    """确定昵称与"是否显式命名"（显式名字跟着人走，自动名跟着 sid 走）。"""
    explicit = _clean(name, 40)
    known = _known_names.get(sid, "")
    if explicit:
        return explicit, True
    if known:
        return known, True
    return default_name(sid, client_id), False


def _touch(entry: dict) -> None:
    entry["last_seen"] = _now()
    entry["online"] = True


def _entry(client_id: Any, sid: Any = "default", name: Any = "", *,
           joined: str | None = None) -> dict:
    cid = _clean(client_id, 64)
    sid_s = _clean(sid) or "default"
    chosen, named = _name_for(sid_s, cid, name)
    return {
        "client_id": cid,
        "sid": sid_s,
        "name": chosen,
        "named": named,
        "online": True,
        "last_seen": _now(),
        "joined": joined or _now(),
    }


def register(client_id: Any, sid: Any = "default", name: Any = "") -> dict:
    """登记一条 ws 连接；重复登记同一 client_id 会刷新而不是新增。"""
    cid = _clean(client_id, 64)
    if not cid:
        return {}
    sid_s = _clean(sid) or "default"
    explicit = _clean(name, 40)
    with _lock:
        if explicit:
            _known_names[sid_s] = explicit
        old = _clients.get(cid)
        entry = _entry(cid, sid_s, explicit, joined=(old or {}).get("joined"))
        _clients[cid] = entry
        if len(_clients) > MAX_CLIENTS:
            # 丢最早活跃的连接：新连接比一个可能已经消失的旧连接更值得保留
            stale = sorted(_clients.items(), key=lambda kv: kv[1].get("last_seen", ""))
            for key, _ in stale[:len(_clients) - MAX_CLIENTS]:
                _clients.pop(key, None)
        return dict(entry)


def bind(client_id: Any, sid: Any) -> bool:
    """更新连接的当前 sid（切会话/工作区、或绑定声明到达时调用）。

    返回 sid 是否真的变了 —— 调用方据此决定要不要重新广播在线列表，
    避免每次 ``bind`` 心跳都把所有客户端刷一遍。
    """
    cid = _clean(client_id, 64)
    if not cid:
        return False
    new_sid = _clean(sid) or "default"
    with _lock:
        entry = _clients.get(cid)
        if entry is None:
            register(cid, new_sid)
            return True
        if entry.get("sid") == new_sid:
            _touch(entry)
            return False
        entry["sid"] = new_sid
        known = _known_names.get(new_sid, "")
        if known:
            entry["name"], entry["named"] = known, True
        elif not entry.get("named"):
            # 自动名跟着 sid 走：换了工作区就该显示新的临时名，而不是上一个的
            entry["name"] = default_name(new_sid, cid)
        _touch(entry)
        return True


def set_name(client_id: Any, name: Any) -> dict:
    """给单条连接设置显式昵称（sid 级持久化由调用方另做）。"""
    cid = _clean(client_id, 64)
    name_s = _clean(name, 40)
    with _lock:
        entry = _clients.get(cid)
        if not entry or not name_s:
            return dict(entry or {})
        entry["name"] = name_s
        entry["named"] = True
        _touch(entry)
        _known_names[entry.get("sid", "")] = name_s
        return dict(entry)


def unregister(client_id: Any) -> dict | None:
    """注销连接（断开 / 关闭页签）；返回被移除的登记项。"""
    cid = _clean(client_id, 64)
    if not cid:
        return None
    with _lock:
        return _clients.pop(cid, None)


def rename(sid: Any, name: Any) -> int:
    """把某个 sid 下所有连接的昵称改为 name（昵称按人，不按标签页）。

    即使当前没有在线连接也记住（用户先设名字、再开页面也能显示），
    返回被就地更新的连接数。
    """
    sid_s = _clean(sid)
    name_s = _clean(name, 40)
    if not sid_s or not name_s:
        return 0
    with _lock:
        _known_names[sid_s] = name_s
        changed = 0
        for entry in _clients.values():
            if entry.get("sid") == sid_s:
                entry["name"] = name_s
                entry["named"] = True
                _touch(entry)
                changed += 1
        return changed


def load_names(mapping: Iterable[tuple[str, str]] | dict[str, str]) -> int:
    """载入持久化昵称（启动时从 ``team_members.json`` 灌入），返回条数。"""
    items = mapping.items() if isinstance(mapping, dict) else list(mapping or [])
    count = 0
    with _lock:
        for raw_sid, raw_name in items:
            sid_s, name_s = _clean(raw_sid), _clean(raw_name, 40)
            if sid_s and name_s:
                _known_names[sid_s] = name_s
                count += 1
    return count


def known_names() -> dict[str, str]:
    """当前已知的显式昵称快照（诊断/测试用）。"""
    with _lock:
        return dict(_known_names)


def list_members() -> list[dict]:
    """在线成员列表：**按 sid 聚合**（多标签页 = 一个人）。

    每项：``{sid,name,online,last_seen,joined,tabs}``。同名/同 sid 的多个连接取
    最近活跃的那个名字与时间；``tabs`` 是这个人当前开着的页面数。
    """
    with _lock:
        groups: dict[str, list[dict]] = {}
        for entry in _clients.values():
            groups.setdefault(entry.get("sid", ""), []).append(dict(entry))
    members: list[dict] = []
    for sid, entries in groups.items():
        entries.sort(key=lambda e: e.get("last_seen", ""), reverse=True)
        name = next((e.get("name", "") for e in entries if e.get("name")),
                    default_name(sid, entries[0].get("client_id", "")))
        members.append({
            "sid": sid,
            "name": name,
            "online": any(bool(e.get("online")) for e in entries),
            "last_seen": max((e.get("last_seen", "") for e in entries), default=""),
            "joined": min((e.get("joined", "") for e in entries), default=""),
            "tabs": len(entries),
        })
    # 在线的排前面；同状态按名字，名字相同按 sid —— 输出稳定，前端不会乱跳
    members.sort(key=lambda m: (not m["online"], m["name"], m["sid"]))
    return members


def count() -> int:
    """当前在线成员数（按人，不按连接）。"""
    return len(list_members())


def client_count() -> int:
    """当前登记的连接数（多标签页分开计；诊断/测试用）。"""
    with _lock:
        return len(_clients)


def reset() -> None:
    """清空在线表与已知昵称（测试用：进程级的表必须能互相隔离）。"""
    with _lock:
        _clients.clear()
        _known_names.clear()
