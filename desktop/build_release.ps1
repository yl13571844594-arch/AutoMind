# AutoMind 桌面版一键发布流水线：
#   前端构建（可选）→ PyInstaller 冻结 → 签名主程序 → Inno 安装器 → 签名安装包 → 校验
#
# 用法：
#   .\build_release.ps1                 # 完整流水线（有证书则自动签名）
#   .\build_release.ps1 -SkipWeb        # 跳过前端构建（web 未改动时提速）
#   .\build_release.ps1 -NoSign         # 明确跳过签名（开发构建）
#
# 签名开关：配置了 AUTOMIND_CERT_THUMBPRINT 或 AUTOMIND_CERT_PFX 即自动签名；
#           证书未到手时无需任何改动，产出未签名构建。

param(
    [switch]$SkipWeb,
    [switch]$NoSign
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$root = Split-Path $PSScriptRoot -Parent

$canSign = (-not $NoSign) -and ($env:AUTOMIND_CERT_THUMBPRINT -or $env:AUTOMIND_CERT_PFX)
Write-Host ("== AutoMind 发布构建 == 签名: " + $(if ($canSign) { "启用" } else { "跳过（未配置证书）" }))

# 0) 版本一致性检查（__init__.py 与 installer.iss）
$ver = (Select-String -Path "$root\automind\__init__.py" -Pattern '__version__ = "([\d.]+)"').Matches[0].Groups[1].Value
$issVer = (Select-String -Path "installer.iss" -Pattern '#define AppVersion "([\d.]+)"').Matches[0].Groups[1].Value
if ($ver -ne $issVer) { throw "版本不一致：__init__.py=$ver installer.iss=$issVer — 请先对齐" }
Write-Host "版本: v$ver"

# 1) 前端构建
if (-not $SkipWeb) {
    Write-Host "`n[1/5] 构建 React 前端…"
    Push-Location "$root\web"
    pnpm build
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "前端构建失败" }
    Pop-Location
} else { Write-Host "`n[1/5] 跳过前端构建（-SkipWeb）" }

# 2) PyInstaller
Write-Host "`n[2/5] PyInstaller 冻结…"
Get-Process AutoMind -ErrorAction SilentlyContinue | Stop-Process -Force -Confirm:$false
pyinstaller automind.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }

# 3) 签名主程序
if ($canSign) {
    Write-Host "`n[3/5] 签名 AutoMind.exe…"
    & "$PSScriptRoot\sign.ps1" -Path "dist\AutoMind\AutoMind.exe"
} else { Write-Host "`n[3/5] 跳过主程序签名" }

# 4) Inno 安装器（签名启用时经 /DSIGN 让 Inno 调 sign.ps1 签 Setup.exe）
Write-Host "`n[4/5] 编译安装器…"
$iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "找不到 ISCC.exe — winget install JRSoftware.InnoSetup" }
if ($canSign) {
    & $iscc installer.iss /DSIGN `
        "/Ssignps1=powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\sign.ps1`" -Path `$f"
} else {
    & $iscc installer.iss
}
if ($LASTEXITCODE -ne 0) { throw "安装器编译失败" }

# 5) 终验
Write-Host "`n[5/5] 校验产物…"
$setup = "Output\AutoMind-Setup-$ver.exe"
if (-not (Test-Path $setup)) { throw "安装包缺失: $setup" }
if ($canSign) {
    & "$PSScriptRoot\sign.ps1" -Path "dist\AutoMind\AutoMind.exe", $setup -VerifyOnly
}
$size = [math]::Round((Get-Item $setup).Length / 1MB, 1)
Write-Host "`n✅ 构建完成：$setup（$size MB）" -ForegroundColor Green
if (-not $canSign) {
    Write-Host "提示：证书到手后设置 AUTOMIND_CERT_THUMBPRINT 重跑本脚本即产出全签名版本"
}
