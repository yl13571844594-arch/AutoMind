"""自动更新 — GitHub Releases 检查 / 安装包下载校验 / 桌面静默升级。

流程（桌面版，frozen 模式）：
    1. check()  ：查询 GitHub Releases 最新版本，与本地版本语义比较；
                  结果缓存 6 小时（kv: update_check），可 force 刷新。
    2. apply()  ：下载 AutoMind-Setup-<ver>.exe 到临时目录 →
                  Authenticode 签名校验（自身已签名时强制要求新包
                  签名有效且同一发布者 —— 防降级投毒）→
                  写升级批处理（等本进程退出 → 静默安装 → 重启应用）→
                  延迟退出进程，Inno 同 AppId 原地升级。

pip / 源码模式：check() 正常可用（提示 pip install -U），apply() 拒绝。

安全边界：
    - 仅接受来自本仓库 GitHub Release API 返回的资产下载地址，
      且域名限定 github.com / *.githubusercontent.com；
    - 签名校验用 PowerShell Get-AuthenticodeSignature（无第三方依赖）；
    - 自身未签名（开发/过渡期构建）时降级为"有签名必须有效"。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from automind import __version__
from automind.core.logging import get_logger

logger = get_logger("automind.updater")

GITHUB_REPO = "yl13571844594-arch/AutoMind"
_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_ASSET_RE = re.compile(r"^AutoMind-Setup-([\d.]+)\.exe$")
_ALLOWED_HOSTS = ("github.com", "api.github.com", "objects.githubusercontent.com",
                  "release-assets.githubusercontent.com", "githubusercontent.com")
_CACHE_TTL = 6 * 3600
_UA = {"User-Agent": f"AutoMind/{__version__} (update-check)",
       "Accept": "application/vnd.github+json"}

_state: dict = {"status": "idle", "progress": 0, "error": ""}   # apply 进度
_lock = threading.Lock()


def _open(url_or_req, timeout: float = 10):
    """打开 URL：先走系统代理（urllib 默认），失败再直连重试一次。

    国内环境系统代理时常只对浏览器生效/波动，双通道显著提高可用性。
    """
    try:
        return urllib.request.urlopen(url_or_req, timeout=timeout)
    except Exception:
        direct = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))
        return direct.open(url_or_req, timeout=timeout)


def _parse_ver(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip().lstrip("vV").split("."))
    except ValueError:
        return (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    return _parse_ver(remote) > _parse_ver(local)


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


# ── 检查 ─────────────────────────────────────────────────


def check(force: bool = False) -> dict:
    """检查最新版本（带 6h 缓存）。永不抛异常，失败返回 error 字段。"""
    from automind.core.db import get_db
    db = get_db()
    cached = db.kv_get("update_check", {})
    if (not force and cached
            and time.time() - cached.get("checked_at", 0) < _CACHE_TTL):
        return {**cached, "cached": True, "mode": "desktop" if _is_frozen() else "pip",
                "current": __version__}
    try:
        req = urllib.request.Request(_API_LATEST, headers=_UA)
        with _open(req) as r:
            rel = json.loads(r.read())
        latest = (rel.get("tag_name") or "").lstrip("vV")
        asset = next(
            (a for a in rel.get("assets", [])
             if _ASSET_RE.match(a.get("name", ""))), None)
        result = {
            "available": is_newer(latest),
            "latest": latest,
            "notes": (rel.get("body") or "")[:4000],
            "published_at": rel.get("published_at", ""),
            "asset_url": asset["browser_download_url"] if asset else "",
            "asset_size": asset.get("size", 0) if asset else 0,
            "release_url": rel.get("html_url", ""),
            "checked_at": time.time(),
            "error": "",
        }
        db.kv_set("update_check", result)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # 仓库尚未发布任何 Release（仅有 tag）→ 视为已是最新
            result = {"available": False, "latest": "", "error": "",
                      "notes": "", "asset_url": "", "release_url": "",
                      "checked_at": time.time()}
            from automind.core.db import get_db as _gdb
            _gdb().kv_set("update_check", result)
        else:
            logger.warning("update_check_failed", error=str(e))
            result = {"available": False, "latest": "", "error": f"检查失败：{e}",
                      "checked_at": time.time()}
    except Exception as e:
        logger.warning("update_check_failed", error=str(e))
        result = {"available": False, "latest": "", "error": f"检查失败：{e}",
                  "checked_at": time.time()}
    return {**result, "cached": False, "mode": "desktop" if _is_frozen() else "pip",
            "current": __version__}


# ── 签名校验 ─────────────────────────────────────────────


def _signature_info(path: str) -> tuple[str, str]:
    """返回 (状态, 发布者 Subject)；非 Windows / 查询失败返回 ("Unknown", "")。"""
    if sys.platform != "win32":
        return "Unknown", ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$s = Get-AuthenticodeSignature -FilePath '{path}';"
             "$s.Status.ToString() + '|' + "
             "$(if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { '' })"],
            capture_output=True, text=True, timeout=30, check=False)
        status, _, subject = out.stdout.strip().partition("|")
        return status or "Unknown", subject
    except Exception as e:
        logger.warning("signature_query_failed", error=str(e))
        return "Unknown", ""


def _verify_download(setup_path: str) -> tuple[bool, str]:
    """签名策略：自身已签名 → 新包必须签名有效且同发布者（防降级/投毒）；
    自身未签名（过渡期）→ 新包若带签名则必须有效，未签名放行并记录。"""
    my_status, my_subject = ("Unknown", "")
    if _is_frozen():
        my_status, my_subject = _signature_info(sys.executable)
    new_status, new_subject = _signature_info(setup_path)
    if my_status == "Valid":
        if new_status != "Valid":
            return False, f"更新包签名无效（{new_status}），已拒绝安装"
        if new_subject != my_subject:
            return False, "更新包发布者与当前程序不一致，已拒绝安装"
        return True, "签名校验通过（同发布者）"
    if new_status == "Valid":
        return True, f"更新包已签名（{new_subject.split(',')[0]}）"
    if new_status in ("Unknown", "NotSigned", "UnknownError"):
        logger.warning("update_unsigned", status=new_status)
        return True, "更新包未签名（当前程序亦未签名，放行）"
    return False, f"更新包签名状态异常（{new_status}），已拒绝安装"


# ── 应用更新（桌面版）─────────────────────────────────────


def state() -> dict:
    with _lock:
        return dict(_state)


def _set(status: str, progress: int = 0, error: str = "") -> None:
    with _lock:
        _state.update({"status": status, "progress": progress, "error": error})


def apply_update() -> dict:
    """启动后台升级流程；立即返回，前端轮询 state()。"""
    if not _is_frozen():
        return {"error": "当前为 pip/源码运行模式，请使用 pip install -U automind-agent 升级"}
    info = check(force=False)
    if not info.get("available") or not info.get("asset_url"):
        return {"error": "没有可用的更新（或缺少安装包资产）"}
    host = urlparse(info["asset_url"]).hostname or ""
    if not (host in _ALLOWED_HOSTS or host.endswith(".githubusercontent.com")):
        return {"error": f"下载地址域名不受信任：{host}"}
    with _lock:
        if _state["status"] in ("downloading", "verifying", "installing"):
            return {"error": "更新已在进行中"}
        _state.update({"status": "downloading", "progress": 0, "error": ""})
    threading.Thread(target=_apply_worker, args=(info,), daemon=True,
                     name="automind-updater").start()
    return {"status": "started", "latest": info["latest"]}


def _apply_worker(info: dict) -> None:
    try:
        tmp = Path(tempfile.gettempdir()) / "AutoMindUpdate"
        tmp.mkdir(parents=True, exist_ok=True)
        setup = tmp / f"AutoMind-Setup-{info['latest']}.exe"
        # 1) 下载（带进度）
        req = urllib.request.Request(info["asset_url"], headers=_UA)
        with _open(req, timeout=30) as r, open(setup, "wb") as f:
            total = int(r.headers.get("Content-Length") or info.get("asset_size") or 0)
            done = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    _set("downloading", int(done / total * 100))
        logger.info("update_downloaded", path=str(setup), bytes=done)
        # 2) 签名校验
        _set("verifying", 100)
        ok, msg = _verify_download(str(setup))
        logger.info("update_verify", ok=ok, msg=msg)
        if not ok:
            _set("error", 0, msg)
            return
        # 3) 升级批处理：等本进程退出 → 静默安装 → 重启
        pid = str(os.getpid())
        exe = sys.executable
        bat = tmp / "apply_update.bat"
        bat.write_text(
            "@echo off\r\n"
            ":wait\r\n"
            f"tasklist /FI \"PID eq {pid}\" 2>nul | find \"{pid}\" >nul && "
            "(timeout /t 1 /nobreak >nul & goto wait)\r\n"
            f"\"{setup}\" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART\r\n"
            f"start \"\" \"{exe}\"\r\n"
            "del \"%~f0\"\r\n",
            encoding="gbk", errors="replace")
        import subprocess as sp
        sp.Popen(["cmd", "/c", str(bat)],
                 creationflags=(sp.CREATE_NO_WINDOW | sp.DETACHED_PROCESS
                                | sp.CREATE_NEW_PROCESS_GROUP),
                 close_fds=True)
        _set("installing", 100)
        # 4) 给前端留出展示时间后退出（批处理接管）
        def _exit() -> None:
            time.sleep(2.5)
            logger.info("update_exit_for_install")
            os._exit(0)
        threading.Thread(target=_exit, daemon=True).start()
    except Exception as e:
        logger.warning("update_apply_failed", error=str(e))
        _set("error", 0, f"更新失败：{e}")
