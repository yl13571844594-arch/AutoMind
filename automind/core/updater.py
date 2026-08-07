"""自动更新 — GitHub Releases 检查 / 安装包下载校验 / 桌面静默升级。

流程（桌面版，frozen 模式）：
    1. check()  ：查询 GitHub Releases 最新版本，与本地版本语义比较；
                  结果缓存 6 小时（kv: update_check），可 force 刷新。
    2. apply()  ：多镜像并发择优 + 断点续传下载安装包 → 三重校验
                  （字节数 / SHA256 / Authenticode 签名）→ 写升级批处理
                  （等本进程退出 → 静默安装 → 重启应用；装失败则拉回旧版本）
                  → 延迟退出进程，Inno 同 AppId 原地升级。

pip / 源码模式：check() 正常可用（提示 pip install -U），apply() 拒绝。

下载可靠性（国内直连 GitHub 常年慢且易断，是升级失败的主因）：
    - 多镜像候选并发探测，取**最先响应**的那个下载，慢/挂的镜像不拖累；
    - HTTP Range 断点续传 + 自动换镜像重试，断链不必从头再来；
    - 读超时即视为卡死并重试，不会永远挂在一个不再吐数据的连接上。

安全边界：
    - 版本与资产元数据（大小、SHA256）**只认 GitHub 官方 API**，镜像仅用于
      搬运字节，不参与"该装哪个版本"的决策；
    - 下载完成后三重校验：字节数（防截断）→ SHA256（防篡改/损坏，基线取自
      官方 API 的 SHA256SUMS 资产）→ Authenticode 签名（防投毒，加密级
      保证：自身已签名时要求新包签名有效且同一发布者）；
      故即便某个镜像作恶，也无法让被改过的包通过校验；
    - 签名校验用 PowerShell Get-AuthenticodeSignature（无第三方依赖）；
    - 自身未签名（开发/过渡期构建）时降级为"有签名必须有效"。
"""

from __future__ import annotations

import concurrent.futures as _cf
import hashlib
import json
import locale
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from automind import __version__
from automind.core.logging import get_logger

logger = get_logger("automind.updater")

GITHUB_REPO = "yl13571844594-arch/AutoMind"
_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# 各平台的安装包命名（Windows 才支持一键静默升级，其余平台给下载地址）
_ASSET_PATTERNS = {
    "win32": re.compile(r"^AutoMind-Setup-([\d.]+)\.exe$"),
    "darwin": re.compile(r"^AutoMind-([\d.]+)\.dmg$"),
    "linux": re.compile(r"^automind_([\d.]+)_amd64\.deb$"),
}
_ASSET_RE = _ASSET_PATTERNS["win32"]   # 兼容旧引用
_SUMS_ASSET = "SHA256SUMS"

_ALLOWED_HOSTS = ("github.com", "api.github.com", "objects.githubusercontent.com",
                  "release-assets.githubusercontent.com", "githubusercontent.com")
_CACHE_TTL = 6 * 3600
_UA = {"User-Agent": f"AutoMind/{__version__} (update-check)",
       "Accept": "application/vnd.github+json"}

# ── 下载镜像 ─────────────────────────────────────────────
# 直连 GitHub 在国内常年 <0.5MB/s 且易断，30MB+ 的安装包因此频繁下载失败。
# 这些是公共 GitHub release 反代，用法为前缀拼接原始 URL。
# 空串 = 直连，始终作为候选之一（境外/公司网络下直连往往最快）。
# 覆盖方式：环境变量 AUTOMIND_UPDATE_MIRRORS（逗号分隔；填 "direct" 只走直连）。
# 安全性由下载后的三重校验保证，见模块文档"安全边界"。
_DEFAULT_MIRRORS = (
    "",
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "https://gh.llkk.cc/",
    "https://ghproxy.net/",
)

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


def mirrors() -> tuple[str, ...]:
    """镜像前缀列表（含直连）。环境变量 AUTOMIND_UPDATE_MIRRORS 可覆盖。"""
    raw = os.environ.get("AUTOMIND_UPDATE_MIRRORS", "").strip()
    if not raw:
        return _DEFAULT_MIRRORS
    if raw.lower() in ("direct", "none", "off"):
        return ("",)
    out = []
    for part in raw.split(","):
        p = part.strip()
        if p in ("direct", '""', "''"):
            p = ""
        elif p and not p.endswith("/"):
            p += "/"
        if p not in out:
            out.append(p)
    return tuple(out) or _DEFAULT_MIRRORS


def mirror_candidates(url: str) -> list[str]:
    """把官方下载地址展开成"直连 + 各镜像"的候选列表。

    只对 github.com 的 release 下载地址套镜像 —— 其它地址原样返回，避免把
    不相干的请求（尤其是 API 请求）也代理出去。
    """
    host = (urlparse(url).hostname or "").lower()
    if host != "github.com" or "/releases/download/" not in url:
        return [url]
    seen, out = set(), []
    for prefix in mirrors():
        cand = prefix + url if prefix else url
        if cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def _probe_fastest(urls: list[str], timeout: float = 8.0) -> str:
    """并发探测候选地址，返回**最先**吐出响应头的那个。

    比"按固定顺序逐个试"好在：镜像可用性因地区/时段剧烈波动，固定顺序会被
    队首的慢镜像拖死；这里让网络自己投票，慢的直接出局。
    """
    if len(urls) <= 1:
        return urls[0]

    def probe(u: str) -> str:
        req = urllib.request.Request(u, headers={**_UA, "Range": "bytes=0-0"})
        with _open(req, timeout=timeout) as r:
            r.read(1)
        return u

    with _cf.ThreadPoolExecutor(max_workers=len(urls)) as ex:
        futs = {ex.submit(probe, u): u for u in urls}
        try:
            for fut in _cf.as_completed(futs, timeout=timeout + 2):
                try:
                    winner = fut.result()
                except Exception:
                    continue
                for f in futs:      # 其余探测无需再等
                    f.cancel()
                logger.info("update_mirror_selected", url=winner)
                return winner
        except (TimeoutError, _cf.TimeoutError):
            pass
    return urls[0]   # 全部探测失败 → 回到直连，让正式下载去报真实错误


def _parse_ver(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip().lstrip("vV").split("."))
    except ValueError:
        return (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    return _parse_ver(remote) > _parse_ver(local)


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _platform_key() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform if sys.platform in _ASSET_PATTERNS else "linux"


def can_auto_install() -> bool:
    """是否支持一键静默升级（仅 Windows 冻结包；其余平台只给下载地址）。"""
    return _is_frozen() and sys.platform == "win32"


def _fetch_sha256(assets: list[dict], filename: str) -> str:
    """从 Release 的 SHA256SUMS 资产里取某个安装包的哈希；取不到返回空串。

    **直连官方地址**取（不走镜像）：这是校验镜像内容的基线，自己必须来自可信源。
    格式为 ``<hash> *<平台目录>/<文件名>``，按 basename 匹配。
    """
    sums = next((a for a in assets if a.get("name") == _SUMS_ASSET), None)
    if not sums:
        return ""
    try:
        req = urllib.request.Request(sums["browser_download_url"], headers=_UA)
        with _open(req, timeout=15) as r:
            text = r.read(64 * 1024).decode("utf-8", "replace")
    except Exception as e:
        logger.warning("update_sums_fetch_failed", error=str(e))
        return ""
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        digest, name = parts[0], parts[1].lstrip("*").strip()
        if PurePosixPath(name).name == filename and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            return digest.lower()
    return ""


# ── 检查 ─────────────────────────────────────────────────


def check(force: bool = False) -> dict:
    """检查最新版本（带 6h 缓存）。永不抛异常，失败返回 error 字段。"""
    from automind.core.db import get_db
    db = get_db()
    cached = db.kv_get("update_check", {})
    if (not force and cached
            and time.time() - cached.get("checked_at", 0) < _CACHE_TTL):
        return {**cached, "cached": True, "mode": "desktop" if _is_frozen() else "pip",
                "current": __version__, "can_auto_install": can_auto_install()}
    try:
        req = urllib.request.Request(_API_LATEST, headers=_UA)
        with _open(req) as r:
            rel = json.loads(r.read())
        latest = (rel.get("tag_name") or "").lstrip("vV")
        assets = rel.get("assets", [])
        pattern = _ASSET_PATTERNS[_platform_key()]
        asset = next((a for a in assets if pattern.match(a.get("name", ""))), None)
        result = {
            "available": is_newer(latest),
            "latest": latest,
            "notes": (rel.get("body") or "")[:4000],
            "published_at": rel.get("published_at", ""),
            "asset_url": asset["browser_download_url"] if asset else "",
            "asset_name": asset.get("name", "") if asset else "",
            "asset_size": asset.get("size", 0) if asset else 0,
            # 校验基线随元数据一起缓存，apply 时不必再联网取一遍
            "asset_sha256": (_fetch_sha256(assets, asset["name"]) if asset else ""),
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
            "current": __version__, "can_auto_install": can_auto_install()}


def check_async(force: bool = True) -> None:
    """后台预热版本检查（桌面版启动时调用）。

    界面在启动约 3 秒后询问 /api/update/check；若那时缓存已过期，用户会先看到
    一次几秒的等待、甚至因超时而**根本收不到新版本提示**。启动即在后台刷新，
    界面拿到的就是新鲜结果，"打开应用即提示升级"才真正成立。
    """
    def _run() -> None:
        try:
            info = check(force=force)
            if info.get("available"):
                logger.info("update_available", latest=info.get("latest"))
        except Exception as e:      # 离线等场景静默
            logger.debug("update_prefetch_failed", error=str(e))

    threading.Thread(target=_run, name="automind-update-check", daemon=True).start()


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


def _set(status: str, progress: int = 0, error: str = "", **extra) -> None:
    with _lock:
        _state.update({"status": status, "progress": progress, "error": error})
        _state.update(extra)


def apply_update() -> dict:
    """启动后台升级流程；立即返回，前端轮询 state()。"""
    if not _is_frozen():
        return {"error": "当前为 pip/源码运行模式，请使用 pip install -U automind-agent 升级"}
    if not can_auto_install():
        return {"error": "本平台暂不支持一键升级，请到发布页下载新版安装包"}
    # 强制刷新：缓存里的 asset_url/校验基线可能已过期（重新发包会换 URL）。
    # 刷新失败时退回缓存结果 —— 检查失败不写缓存，故这里拿到的仍是上次的好数据，
    # 一次网络抖动不该让本来能装的升级直接失败。
    info = check(force=True)
    if not info.get("asset_url"):
        info = check(force=False)
    if not info.get("available") or not info.get("asset_url"):
        return {"error": "没有可用的更新（或缺少安装包资产）"}
    host = urlparse(info["asset_url"]).hostname or ""
    if not (host in _ALLOWED_HOSTS or host.endswith(".githubusercontent.com")):
        return {"error": f"下载地址域名不受信任：{host}"}
    with _lock:
        if _state["status"] in ("downloading", "verifying", "installing"):
            return {"error": "更新已在进行中"}
        _state.update({"status": "downloading", "progress": 0, "error": "",
                       "downloaded": 0, "total": info.get("asset_size", 0),
                       "speed": 0.0, "attempt": 0})
    threading.Thread(target=_apply_worker, args=(info,), daemon=True,
                     name="automind-updater").start()
    return {"status": "started", "latest": info["latest"]}


# ── 下载（多镜像 + 断点续传）──────────────────────────────

_MAX_ATTEMPTS = 6           # 换镜像重试上限
_READ_TIMEOUT = 30.0        # 读超时：超过即视为连接卡死，重试而不是干等
_CHUNK = 256 * 1024


def _download(url: str, dest: Path, expected_size: int) -> int:
    """下载到 dest，支持断点续传与自动换镜像；返回最终字节数。

    每次重试都从**已落盘的字节数**继续（HTTP Range），30MB 的包在不稳定网络
    下不必从头再来 —— 这是"下载总是失败"最直接的解药。
    """
    candidates = mirror_candidates(url)
    chosen = _probe_fastest(candidates)
    # 探测胜出者优先，其余作为失败后的轮换池
    pool = [chosen] + [u for u in candidates if u != chosen]
    last_err: Exception | None = None

    for attempt in range(_MAX_ATTEMPTS):
        target = pool[attempt % len(pool)]
        pos = dest.stat().st_size if dest.exists() else 0
        if expected_size and pos >= expected_size:
            return pos
        headers = dict(_UA)
        if pos:
            headers["Range"] = f"bytes={pos}-"
        try:
            req = urllib.request.Request(target, headers=headers)
            with _open(req, timeout=_READ_TIMEOUT) as r:
                # 服务端忽略 Range（200 而非 206）→ 只能从头写，别把两段拼坏
                if pos and r.status != 206:
                    pos = 0
                total = expected_size or int(r.headers.get("Content-Length") or 0) + pos
                mode = "ab" if pos else "wb"
                done, t0, last_report = pos, time.time(), 0.0
                with open(dest, mode) as f:
                    while True:
                        chunk = r.read(_CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if now - last_report >= 0.4:
                            last_report = now
                            elapsed = max(now - t0, 1e-6)
                            _set("downloading",
                                 int(done / total * 100) if total else 0,
                                 downloaded=done, total=total,
                                 speed=round((done - pos) / elapsed / 1024 / 1024, 2),
                                 attempt=attempt + 1,
                                 mirror=urlparse(target).hostname or "")
            final = dest.stat().st_size
            if not expected_size or final >= expected_size:
                return final
            last_err = OSError(f"下载不完整（{final}/{expected_size} 字节）")
            logger.warning("update_download_short", got=final, want=expected_size)
        except Exception as e:
            last_err = e
            logger.warning("update_download_retry", attempt=attempt + 1,
                           mirror=urlparse(target).hostname or "", error=str(e))
        _set("downloading", _state.get("progress", 0), attempt=attempt + 2)
        time.sleep(min(2 ** attempt, 8))

    raise OSError(f"下载失败（已重试 {_MAX_ATTEMPTS} 次）：{last_err}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _verify_integrity(setup: Path, info: dict) -> tuple[bool, str]:
    """字节数 + SHA256 校验（签名校验之前跑，先排除"下坏了"再谈"是不是真的"）。"""
    want_size = int(info.get("asset_size") or 0)
    got_size = setup.stat().st_size
    if want_size and got_size != want_size:
        return False, f"安装包大小不符（{got_size}/{want_size} 字节），可能下载中断"
    want_hash = (info.get("asset_sha256") or "").lower()
    if want_hash:
        got = _sha256(setup)
        if got != want_hash:
            logger.warning("update_sha256_mismatch", want=want_hash, got=got)
            return False, "安装包校验和不符，已拒绝安装（请重试或改用官网下载）"
        return True, "SHA256 校验通过"
    return True, "未提供校验和，跳过（仍会做签名校验）"


def _install_script(tmp: Path, setup: Path, exe: str, pid: str) -> Path:
    """生成升级批处理。

    "点了升级，应用就再也没回来"的**真凶不在这个函数里**，而在
    :func:`_apply_worker` 启动本脚本的方式上（未显式给标准流句柄，详见那里的注释）。

    此处以 ``newline=""`` 写入属于顺带修的隐患：``Path.write_text()`` 是文本模式，
    会把源串 ``\\r\\n`` 里的 ``\\n`` 再转成 ``\\r\\n``，落盘变成 ``\\r\\r\\n``。
    实测 cmd 能容忍这种行尾（不是本次故障的原因），但没有理由留着 ——
    同类换行转换在本仓库已经坑过一次（SHA256SUMS）。

    另把这个脚本做成"无论如何都不会把用户晾在没有应用可用的状态"：
      · 等旧进程退出设上限，卡住也会继续，不会无限等；
      · ``timeout`` 需要控制台，而本批处理是以 DETACHED_PROCESS 启动的（无控制台），
        改用 ``ping`` 延时，否则那行每次都报错、循环空转烧 CPU；
      · 静默安装失败时**自动改用可见模式重试一次** —— 装在 Program Files 的用户
        需要 UAC 提权，而 ``/VERYSILENT`` 下提权界面出不来，必然静默失败；
      · 最后无论成败都拉起应用（新版或旧版），并保留安装日志便于追溯。
    """
    bat = tmp / "apply_update.bat"
    log = tmp / "install.log"
    lines = [
        "@echo off",
        "setlocal",
        f'set "SETUP={setup}"',
        f'set "APPEXE={exe}"',
        f'set "APPNAME={Path(exe).name}"',
        f'set "LOG={log}"',
        "set RC=0",
        "set /a N=0",
        ":wait",
        # 旧进程还在就等；等不到也不能无限卡着（上限约 120 秒）
        f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul || goto run',
        "set /a N+=1",
        "if %N% GEQ 120 goto run",
        "ping -n 2 127.0.0.1 >nul",
        "goto wait",
        ":run",
        # 主进程 PID 退出不等于文件不再被占用：托盘/子进程、用户另开的第二个窗口
        # 都会让 Inno 装不进去。而 /SUPPRESSMSGBOXES 下"请先关闭应用"的提示弹不出来，
        # Inno 只能以退出码 5（已取消）放弃 —— 表现就是"点了升级却什么也没装上"。
        # 故安装前先确保没有残留实例，再交给 Inno 的 Restart Manager 兜底。
        'taskkill /IM "%APPNAME%" /F /T >nul 2>&1',
        "ping -n 3 127.0.0.1 >nul",
        '"%SETUP%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /FORCECLOSEAPPLICATIONS /LOG="%LOG%"',
        "set RC=%ERRORLEVEL%",
        # 静默失败多半是需要提权（装在 Program Files）；可见模式才能弹 UAC
        "if not %RC%==0 (",
        '  echo silent_failed rc=%RC%, retry visible >>"%LOG%"',
        '  "%SETUP%" /SILENT /NORESTART /FORCECLOSEAPPLICATIONS /LOG="%LOG%.retry"',
        ")",
        # 取重试的退出码**必须在块外**：cmd 是把整个 `( ... )` 块一次性解析完再执行的，
        # 写在块里的 `set RC=%ERRORLEVEL%` 中的 %ERRORLEVEL% 在解析那一刻就被展开成
        # "重试之前"的旧值 —— 重试的真实结果根本没被记录下来，日志里的 exit_code
        # 永远是静默安装那次的失败码，装成了也显示失败，排障时直接把人带偏。
        # （另一种解法是 setlocal enabledelayedexpansion + !ERRORLEVEL!，但那会让
        #  路径里的 `!` 被吃掉；块外取值没有这个副作用。）
        "set RC2=%ERRORLEVEL%",
        "if not %RC%==0 set RC=%RC2%",
        # %RC% 与 >> 之间必须留空格：`echo x=0>>f` 里紧挨 > 的数字会被 cmd
        # 当成文件句柄号（0=stdin），结果写出来的是空值而不是退出码。
        'echo exit_code=%RC% >>"%LOG%"',
        # 无论装成没装成都把应用拉回来：装成了是新版，没装成是旧版，
        # 用户至少不会面对一个"点了升级就消失"的程序。
        'if exist "%APPEXE%" start "" "%APPEXE%"',
        # 自删的标准安全写法：先跳出脚本再删，否则 cmd 会继续去读已被删除的
        # 文件而报"找不到批处理文件"并以非零码退出。
        '(goto) 2>nul & del "%~f0"',
    ]
    # newline="" 关掉换行转换；encoding 用当前区域首选（cmd 按系统 ANSI 代码页读，
    # 非中文 Windows 上的路径才不会被写坏 —— 早期硬编码 gbk 会）。
    with open(bat, "w", encoding=locale.getpreferredencoding(False) or "utf-8",
              errors="replace", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return bat


def _spawn_installer(bat: Path, tmp: Path) -> None:
    """脱离本进程启动升级批处理。

    **必须显式给出 stdin/stdout/stderr** —— 这就是"升级后应用消失"的真凶。
    桌面版是无控制台的 GUI 进程，再加 ``DETACHED_PROCESS``，子进程拿不到任何标准
    句柄；不显式指定时 cmd 继承到的是无效句柄，脚本里的 ``2>nul``、``>>"%LOG%"``、
    ``tasklist | find`` 这些重定向和管道全部失败，批处理立刻以退出码 1 夭折 ——
    安装器根本没被执行到，更走不到最后那句"重新拉起应用"。

    复现（父进程 pythonw.exe，脚本含一次管道 + 一次重定向）：
      · 不给句柄 → cmd rc=1，日志和结束标记文件都没生成；
      · 给了句柄 → cmd rc=0，脚本跑到最后一行。

    顺带把输出留到 ``trace.txt``，升级出问题时有据可查。
    """
    import subprocess as sp
    trace = open(tmp / "trace.txt", "wb")  # noqa: SIM115 - 交给子进程，不能用 with
    try:
        sp.Popen(["cmd", "/c", str(bat)],
                 stdin=sp.DEVNULL, stdout=trace, stderr=trace,
                 creationflags=(sp.CREATE_NO_WINDOW | sp.DETACHED_PROCESS
                                | sp.CREATE_NEW_PROCESS_GROUP),
                 close_fds=True)
    finally:
        trace.close()   # 句柄已复制给子进程，父进程这份可以关


def _apply_worker(info: dict) -> None:
    try:
        tmp = Path(tempfile.gettempdir()) / "AutoMindUpdate"
        tmp.mkdir(parents=True, exist_ok=True)
        setup = tmp / (info.get("asset_name") or f"AutoMind-Setup-{info['latest']}.exe")
        # 上次留下的残包若大小已不符，先清掉，免得续传续到一个错的文件上
        if setup.exists() and info.get("asset_size") \
                and setup.stat().st_size > int(info["asset_size"]):
            setup.unlink()

        # 1) 下载（多镜像择优 + 断点续传）
        done = _download(info["asset_url"], setup, int(info.get("asset_size") or 0))
        logger.info("update_downloaded", path=str(setup), bytes=done)

        # 2) 完整性校验（字节数 + SHA256）
        _set("verifying", 100)
        ok, msg = _verify_integrity(setup, info)
        logger.info("update_integrity", ok=ok, msg=msg)
        if not ok:
            setup.unlink(missing_ok=True)   # 坏包必须删，否则续传永远接在坏文件后面
            _set("error", 0, msg)
            return

        # 3) 签名校验（真正的来源认证，镜像作恶到此为止）
        ok, msg = _verify_download(str(setup))
        logger.info("update_verify", ok=ok, msg=msg)
        if not ok:
            setup.unlink(missing_ok=True)
            _set("error", 0, msg)
            return

        # 4) 升级批处理：等本进程退出 → 静默安装 → 重启
        bat = _install_script(tmp, setup, sys.executable, str(os.getpid()))
        _spawn_installer(bat, tmp)
        _set("installing", 100)

        # 5) 给前端留出展示时间后退出（批处理接管）
        def _exit() -> None:
            time.sleep(2.5)
            logger.info("update_exit_for_install")
            os._exit(0)
        threading.Thread(target=_exit, daemon=True).start()
    except Exception as e:
        logger.warning("update_apply_failed", error=str(e))
        _set("error", 0, f"更新失败：{e}")
