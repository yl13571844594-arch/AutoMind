# AutoMind 社区版 GitHub 发布 —— 认证之后一键跑完。
#
# 用法（先且仅需做一次交互式登录）：
#   gh auth login                       # 走浏览器/设备码，凭据进系统凭据管理器
#   .\scripts\release_github.ps1        # 然后跑这个
#
#   .\scripts\release_github.ps1 -SkipPush       # 已经推过 main 时跳过推送
#   .\scripts\release_github.ps1 -NoWait         # 只触发 CI，不等它跑完
#
# 这个脚本做什么：
#   1) 前置检查：gh 已登录 / 版本号一致 / 本机 Windows 包已签名
#   2) 推送 main（只推社区版 —— pro/ 已被 .gitignore 排除）
#   3) 用 workflow_dispatch 触发 desktop-build，产出 macOS .dmg 与 Linux .deb
#   4) 等 CI 跑完并下载产物到 dist/desktop/
#   5) 建**草稿** Release，把三个平台的包 + 校验和一并上传
#
# 为什么建"草稿"而不是直接发布 —— 这是刻意的：
#   publish.yml 与 desktop-build.yml 的触发条件同为 `push: tags v*.*.*`。
#   一旦真正创建 tag，**PyPI 会同时发布**，而 PyPI 版本号一经占用不可撤销
#   （yank 之后也不能重传同版本）。草稿 Release 不创建 tag，因此不会触发任何
#   工作流。你在网页上确认无误后再点 Publish —— 那一刻才会建 tag，
#   也才会发 PyPI。要的就是把这个不可逆动作留在你手里。
#
# Windows 包为何必须本机上传：
#   CI 产的 Windows 包既未签名、也不含内嵌 WebView2 引导器，严格差于本机
#   build_release.ps1 的产物。desktop-build.yml 因此刻意不 attach 它
#   （历史上出过"CI 未签名版覆盖掉已签名版"的事故）。

param(
    [switch]$SkipPush,
    [switch]$NoWait
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $repoRoot

function Fail($msg) { Write-Host "✗ $msg" -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n== $msg ==" -ForegroundColor Cyan }

# ── 0) 前置检查 ────────────────────────────────────────────
Step "前置检查"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Fail "未安装 GitHub CLI。装：winget install GitHub.cli"
}
# 用 *> 丢弃全部输出流：在 PS5.1 里对原生命令用 2>&1 会把 stderr 每一行包成
# ErrorRecord（NativeCommandError），即便退出码为 0 也会被当成失败。
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
gh auth status *> $null
$authOk = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prevEap
if (-not $authOk) {
    Fail "gh 未登录。先执行：gh auth login（浏览器/设备码登录，不需要手输令牌）"
}
Write-Host "✓ gh 已登录"

$ver = (Select-String -Path "automind\__init__.py" -Pattern '__version__\s*=\s*"([\d.]+)"').Matches[0].Groups[1].Value
if (-not $ver) { Fail "读不出版本号" }
Write-Host "✓ 版本 v$ver"

$winExe = "dist\desktop\AutoMind-Setup-$ver.exe"
if (-not (Test-Path $winExe)) {
    Fail "找不到本机 Windows 安装包 $winExe —— 先跑 desktop\build_release.ps1"
}
$sig = Get-AuthenticodeSignature $winExe
if ($sig.Status -ne "Valid") {
    Fail ("Windows 包签名状态为 $($sig.Status)，拒绝发布未签名包。" +
          "设 AUTOMIND_CERT_THUMBPRINT 后重跑 desktop\build_release.ps1")
}
Write-Host ("✓ Windows 包已签名：" + $sig.SignerCertificate.Subject.Split(',')[0])

# ── 1) 推送社区版 ──────────────────────────────────────────
if (-not $SkipPush) {
    Step "推送 main（仅社区版）"
    $proTracked = (git ls-files pro/ | Measure-Object -Line).Lines
    if ($proTracked -ne 0) {
        Fail "pro/ 下有 $proTracked 个被跟踪的文件 —— 商业包不得同步，请先从索引移除"
    }
    Write-Host "✓ pro/ 零跟踪文件，推送内容仅社区版"
    git push origin main
    if ($LASTEXITCODE -ne 0) { Fail "推送失败" }
    Write-Host "✓ 已推送"
}

# ── 2) 触发三平台构建 ──────────────────────────────────────
Step "触发 desktop-build（workflow_dispatch，不建 tag 故不触发 PyPI）"
gh workflow run desktop-build.yml --ref main
if ($LASTEXITCODE -ne 0) { Fail "触发失败" }
Start-Sleep -Seconds 8
$runId = (gh run list --workflow=desktop-build.yml --limit 1 --json databaseId --jq ".[0].databaseId")
Write-Host "✓ 已触发，run id = $runId"

if ($NoWait) {
    Write-Host "`n-NoWait：不等待。稍后用 gh run watch $runId 查看进度。" -ForegroundColor Yellow
    exit 0
}

Step "等待 CI 完成（macOS 通用二进制较慢，通常 15-25 分钟）"
gh run watch $runId --exit-status
if ($LASTEXITCODE -ne 0) {
    Fail "CI 失败。查看日志：gh run view $runId --log-failed"
}
Write-Host "✓ CI 通过"

# ── 3) 下载产物 ────────────────────────────────────────────
Step "下载 macOS / Linux 产物"
$tmp = Join-Path $env:TEMP "automind-release-$ver"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
gh run download $runId --dir $tmp
if ($LASTEXITCODE -ne 0) { Fail "下载产物失败" }

$dmg = Get-ChildItem $tmp -Recurse -Filter "AutoMind-*.dmg" | Select-Object -First 1
$deb = Get-ChildItem $tmp -Recurse -Filter "automind_*_amd64.deb" | Select-Object -First 1
if (-not $dmg) { Fail "产物里没有 .dmg" }
if (-not $deb) { Fail "产物里没有 .deb" }
Copy-Item $dmg.FullName "dist\desktop\" -Force
Copy-Item $deb.FullName "dist\desktop\" -Force
Write-Host ("✓ " + $dmg.Name + " / " + $deb.Name + " 已存入 dist\desktop\")

# macOS 包是否经过公证 —— 未配 notary secrets 时只是签名未公证
Write-Host "`n提示：若未配置 MAC_NOTARY_* secrets，DMG 为「已签名未公证」，" -ForegroundColor Yellow
Write-Host "      用户首次打开需右键 → 打开。" -ForegroundColor Yellow

# ── 4) 重算校验和 ──────────────────────────────────────────
Step "生成 SHA256SUMS"
$sumFile = "dist\desktop\SHA256SUMS.txt"
$header = @(
    "# AutoMind v$ver 桌面安装包校验和",
    "# Windows 包已用 Certum 代码签名证书签署，含 RFC3161 时间戳。",
    "# macOS/Linux 包由 GitHub Actions 构建（见仓库 desktop-build.yml）。"
)
$lines = @()
foreach ($f in Get-ChildItem "dist\desktop" -Include *.exe,*.dmg,*.deb -Recurse) {
    $h = (Get-FileHash $f.FullName -Algorithm SHA256).Hash.ToLower()
    $lines += "$h  $($f.Name)"
    Write-Host "  $h  $($f.Name)"
}
# 用 UTF8 无 BOM 写，避免下游校验工具把 BOM 当成内容
[System.IO.File]::WriteAllLines((Join-Path $repoRoot $sumFile), ($header + $lines))

# ── 5) 建草稿 Release 并上传 ───────────────────────────────
Step "创建草稿 Release 并上传三平台安装包"
$notes = @"
## AutoMind v$ver

安装包（三平台）：

| 平台 | 文件 | 说明 |
|---|---|---|
| Windows | ``AutoMind-Setup-$ver.exe`` | 已代码签名，内嵌 WebView2 引导器 |
| macOS | ``$($dmg.Name)`` | 通用二进制（Apple Silicon + Intel） |
| Linux | ``$($deb.Name)`` | Debian / Ubuntu，amd64 |

校验和见 ``SHA256SUMS.txt``。完整变更见 [CHANGELOG.md](https://github.com/yl13571844594-arch/AutoMind/blob/main/CHANGELOG.md)。
"@
$notesFile = Join-Path $tmp "notes.md"
[System.IO.File]::WriteAllText($notesFile, $notes)

$assets = @($winExe, "dist\desktop\$($dmg.Name)", "dist\desktop\$($deb.Name)", $sumFile)
gh release create "v$ver" $assets --draft --title "AutoMind v$ver" --notes-file $notesFile
if ($LASTEXITCODE -ne 0) { Fail "创建 Release 失败" }

Write-Host "`n✅ 草稿 Release 已就绪：" -ForegroundColor Green
gh release view "v$ver" --json url --jq ".url"
Write-Host @"

下一步（由你决定，脚本刻意不代劳）：
  · 在网页上核对三个安装包无误后点 Publish release。
  · ⚠ 点 Publish 会创建 tag v$ver，从而**同时触发 PyPI 发布**
    （publish.yml 与 desktop-build.yml 触发条件相同）。
    PyPI 版本号一经占用不可撤销 —— 若本次不想发 PyPI，
    请先在 Actions 页禁用 publish.yml，或保持草稿状态。
"@ -ForegroundColor Yellow
