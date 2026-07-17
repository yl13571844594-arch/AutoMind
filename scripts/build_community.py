"""构建社区版开源发布物（v0.6.0+）。

产物（输出到 dist/）：
    1. automind_agent-<ver>-py3-none-any.whl / .tar.gz  — pip 安装包
    2. automind-community-<ver>-src.zip                 — 开源上传源码包

安全保证：
    - 源码包**白名单**收集（只进开源清单内的文件），商业代码（pro/）、
      运行数据（.automind*/.reasonix）、密钥、内部规划文档一律不进包；
    - 打包后自动审计：任何产物内出现 automind_pro / 许可证密钥即失败。

用法：
    python scripts/build_community.py
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

# ── 开源源码包白名单 ─────────────────────────────────────
# v1.0+：web/ 为 React 前端工程源码（构建产物在 automind/static/dist 随包）
INCLUDE_DIRS = ["automind", "web", "tests", "examples", "demo", "docs", ".github"]
INCLUDE_FILES = [
    "pyproject.toml", "README.md", "README.en.md", "LICENSE", "RELEASE.md",
    "CHANGELOG.md", "CONTRIBUTING.md",
    "使用手册.md", "使用手册.html", "launch.bat",
    "Dockerfile", "docker-compose.yml", ".dockerignore",
    ".gitignore", ".gitattributes",
]
# 目录内仍需排除的模式
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                 "node_modules"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".rar", ".key", ".pem"}
# 任何情况下都不允许进包的敏感/商业标记
FORBIDDEN_NAMES = {"automind_pro", ".automind_config.json", ".automind_license",
                   ".env", "LICENSE-COMMERCIAL.md"}


def _version() -> str:
    ns: dict = {}
    text = (ROOT / "automind" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            exec(line, ns)  # noqa: S102 - 受控源文件
            return ns["__version__"]
    raise RuntimeError("automind/__init__.py 中未找到 __version__")


def _iter_source_files():
    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT)
            if set(rel.parts) & EXCLUDE_PARTS:
                continue
            if p.suffix.lower() in EXCLUDE_SUFFIXES:
                continue
            if p.name in FORBIDDEN_NAMES:
                continue
            yield rel
    for f in INCLUDE_FILES:
        p = ROOT / f
        if p.is_file():
            yield p.relative_to(ROOT)


def build_wheel_sdist() -> None:
    print(">> 构建 wheel + sdist（python -m build）...")
    subprocess.run([sys.executable, "-m", "build", "--outdir", str(DIST)],
                   cwd=ROOT, check=True)


def build_source_zip(version: str) -> Path:
    out = DIST / f"automind-community-{version}-src.zip"
    print(f">> 打包开源源码 {out.name} ...")
    DIST.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        n = 0
        for rel in _iter_source_files():
            z.write(ROOT / rel, f"automind-community-{version}/{rel.as_posix()}")
            n += 1
    print(f"   共 {n} 个文件")
    return out


def audit(version: str) -> None:
    """审计全部产物：不得含商业代码或敏感文件。"""
    print(">> 审计发布产物 ...")
    bad: list[str] = []
    markers = ("automind_pro", "pro/automind_pro", ".automind_config.json",
               ".automind_license", "LICENSE-COMMERCIAL")
    for art in DIST.glob(f"*{version}*"):
        names: list[str] = []
        if art.suffix == ".zip" or art.name.endswith(".whl"):
            with zipfile.ZipFile(art) as z:
                names = z.namelist()
        elif art.name.endswith(".tar.gz"):
            import tarfile
            with tarfile.open(art) as t:
                names = t.getnames()
        for name in names:
            if any(m in name for m in markers):
                bad.append(f"{art.name}: {name}")
    if bad:
        print("!! 审计失败，产物包含禁止内容：")
        for b in bad:
            print("   " + b)
        sys.exit(1)
    print("   审计通过：产物不含商业代码 / 敏感文件")


def main() -> None:
    version = _version()
    print(f"AutoMind 社区版发布构建 v{version}")
    build_wheel_sdist()
    build_source_zip(version)
    audit(version)
    print("\n完成。产物：")
    for art in sorted(DIST.glob(f"*{version}*")):
        print(f"  {art.relative_to(ROOT)}  ({art.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
