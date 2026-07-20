# AutoMind 代码签名脚本 — OV 证书到手后即插即用。
#
# 用法（三选一，按优先级）：
#   1) 证书已导入"当前用户\个人"证书库（推荐，PFX 双击导入一次即可）：
#        $env:AUTOMIND_CERT_THUMBPRINT = "<证书指纹40位HEX>"
#        .\sign.ps1 -Path dist\AutoMind\AutoMind.exe
#   2) PFX 文件直签：
#        $env:AUTOMIND_CERT_PFX = "C:\secure\automind-ov.pfx"
#        $env:AUTOMIND_CERT_PWD = "<pfx密码>"        # 只放环境变量，勿写进任何文件
#        .\sign.ps1 -Path dist\AutoMind\AutoMind.exe
#   3) 批量：.\sign.ps1 -Path a.exe,b.exe
#
# 说明：SHA256 签名 + RFC3161 时间戳（证书过期后签名依然有效）。
#       时间戳服务器按序回退（sectigo → digicert → globalsign）。

param(
    [Parameter(Mandatory = $true)][string[]]$Path,
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"

function Find-SignTool {
    $cached = Get-Command signtool -ErrorAction SilentlyContinue
    if ($cached) { return $cached.Source }
    $kits = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    if (Test-Path $kits) {
        $found = Get-ChildItem $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "x64" } |
            Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    throw ("找不到 signtool.exe — 请安装 Windows SDK（winget install " +
           "Microsoft.WindowsSDK.SignTool 或完整 SDK）后重试")
}

$signtool = Find-SignTool
Write-Host "signtool: $signtool"

if ($VerifyOnly) {
    foreach ($f in $Path) {
        & $signtool verify /pa /v $f
        if ($LASTEXITCODE -ne 0) { throw "签名验证失败: $f" }
    }
    Write-Host "全部签名验证通过 ✓" -ForegroundColor Green
    exit 0
}

# 证书来源
$thumb = $env:AUTOMIND_CERT_THUMBPRINT
$pfx = $env:AUTOMIND_CERT_PFX
if (-not $thumb -and -not $pfx) {
    throw ("未配置证书 — 设置 AUTOMIND_CERT_THUMBPRINT（证书库指纹）或 " +
           "AUTOMIND_CERT_PFX + AUTOMIND_CERT_PWD（PFX 文件）后重试")
}

$tsServers = @(
    "http://timestamp.sectigo.com",
    "http://timestamp.digicert.com",
    "http://timestamp.globalsign.com/tsa/r6advanced1"
)

foreach ($f in $Path) {
    if (-not (Test-Path $f)) { throw "文件不存在: $f" }
    $signed = $false
    foreach ($ts in $tsServers) {
        Write-Host "签名 $f（时间戳: $ts）…"
        if ($thumb) {
            & $signtool sign /fd sha256 /td sha256 /tr $ts /sha1 $thumb $f
        } else {
            & $signtool sign /fd sha256 /td sha256 /tr $ts /f $pfx /p $env:AUTOMIND_CERT_PWD $f
        }
        if ($LASTEXITCODE -eq 0) { $signed = $true; break }
        Write-Warning "时间戳服务器 $ts 失败，换下一个…"
    }
    if (-not $signed) { throw "签名失败: $f（全部时间戳服务器不可用？）" }
    & $signtool verify /pa $f
    if ($LASTEXITCODE -ne 0) { throw "签名后验证失败: $f" }
    Write-Host "已签名 ✓ $f" -ForegroundColor Green
}
