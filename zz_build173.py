"""一次性：用 --no-isolation 构建 1.7.3 发布物（联网装构建依赖会卡住）。用完即删。"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent

# 复用 scripts/build_community.py 里的白名单收集与审计（不重复实现）
spec = importlib.util.spec_from_file_location("bc", ROOT / "scripts" / "build_community.py")
bc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc)

version = bc._version()
print("版本:", version)

print(">> 构建 wheel + sdist（--no-isolation）...")
subprocess.run([sys.executable, "-m", "build", "--outdir", str(bc.DIST), "--no-isolation"],
               cwd=ROOT, check=True)

bc.build_source_zip(version)
bc.audit(version)

print("\n产物：")
for art in sorted(bc.DIST.glob(f"*{version}*")):
    print(f"  {art.name}  ({art.stat().st_size / 1024:.0f} KB)")
