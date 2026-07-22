# AutoMind 代码签名脚本 — Certum SimplySign（云证书）/ 普通证书通用。
#
# 证书来源（按优先级，只用其一）：
#   A) 证书库指纹（推荐；Certum SimplySign 云证书登录后即在证书库）：
#        $env:AUTOMIND_CERT_THUMBPRINT = "0A6C8F03EB71C7E940955388D257CB7EF329C24B"
#   B) PFX 文件（传统证书）：
#        $env:AUTOMIND_CERT_PFX = "C:\secure\cert.pfx"; $env:AUTOMIND_CERT_PWD = "<密码>"
#
# signtool 定位（按优先级）：
#   $env:AUTOMIND_SIGNTOOL（显式路径）→ Windows Kits 自动搜索 → PATH → 报错。
#   无 SDK 时可从 NuGet 包 Microsoft.Windows.SDK.BuildTools 抽取 signtool.exe。
#
# 时间戳网络（本机专属，可选）：
#   $env:AUTOMIND_TS_PROXY = "127.0.0.1:22307"
#   设置后，签名期间临时把 WinHTTP 代理指向它（signtool 的 RFC3161 时间戳走
#   WinHTTP），签完自动还原原状 —— 解决"外网需代理但 WinHTTP 为直连"导致的
#   时间戳不可达。不设则直连。
#
# 用法：
#   .\sign.ps1 -Path dist\AutoMind\AutoMind.exe
#   .\sign.ps1 -Path a.exe,b.exe            # 批量
#   .\sign.ps1 -Path x.exe -VerifyOnly      # 仅验签
#
# 说明：SHA256 摘要 + RFC3161 时间戳（证书过期后签名依然有效）。
#       时间戳服务器按序回退（sectigo → certum → digicert → globalsign）。
#       PIN/OTP 由 SimplySign Desktop 会话处理，本脚本从不经手。

param(
    [Parameter(Mandatory = $true)][string[]]$Path,
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"

function Find-SignTool {
    if ($env:AUTOMIND_SIGNTOOL -and (Test-Path $env:AUTOMIND_SIGNTOOL)) {
        return $env:AUTOMIND_SIGNTOOL
    }
    $cached = Get-Command signtool -ErrorAction SilentlyContinue
    if ($cached) { return $cached.Source }
    foreach ($root in @("${env:ProgramFiles(x86)}\Windows Kits\10\bin",
                        "$env:ProgramFiles\Windows Kits\10\bin",
                        "$env:LOCALAPPDATA\AutoMindBuildTools")) {
        if (Test-Path $root) {
            $found = Get-ChildItem $root -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -like "*x64*" } |
                Sort-Object FullName -Descending | Select-Object -First 1
            if ($found) { return $found.FullName }
        }
    }
    throw ("找不到 signtool.exe — 设 AUTOMIND_SIGNTOOL 指向它，或安装 Windows SDK，" +
           "或从 NuGet 包 Microsoft.Windows.SDK.BuildTools 抽取")
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

$thumb = $env:AUTOMIND_CERT_THUMBPRINT
$pfx = $env:AUTOMIND_CERT_PFX
if (-not $thumb -and -not $pfx) {
    throw ("未配置证书 — 设 AUTOMIND_CERT_THUMBPRINT（证书库指纹，Certum SimplySign 用）" +
           "或 AUTOMIND_CERT_PFX + AUTOMIND_CERT_PWD（PFX 文件）")
}

# 时间戳服务器：sectigo 首选（实测经代理稳定），Certum 官方与其它作回退
$tsServers = @(
    "http://timestamp.sectigo.com",
    "http://time.certum.pl",
    "http://timestamp.digicert.com",
    "http://timestamp.globalsign.com/tsa/r6advanced1"
)

# 可选：签名期间临时切换 WinHTTP 代理（签完还原），解决时间戳不可达
$tsProxy = $env:AUTOMIND_TS_PROXY
$proxyChanged = $false
if ($tsProxy) {
    Write-Host "临时设置 WinHTTP 代理 → $tsProxy（时间戳用，签完还原）"
    netsh winhttp set proxy proxy-server="$tsProxy" bypass-list="localhost;127.0.0.1" | Out-Null
    $proxyChanged = $true
}

try {
    foreach ($f in $Path) {
        if (-not (Test-Path $f)) { throw "文件不存在: $f" }
        $signed = $false
        foreach ($ts in $tsServers) {
            foreach ($try in 1..2) {
                Write-Host "签名 $f（时间戳: $ts，第 $try 次）…"
                if ($thumb) {
                    & $signtool sign /fd sha256 /td sha256 /tr $ts /sha1 $thumb $f
                } else {
                    & $signtool sign /fd sha256 /td sha256 /tr $ts /f $pfx /p $env:AUTOMIND_CERT_PWD $f
                }
                if ($LASTEXITCODE -eq 0) { $signed = $true; break }
            }
            if ($signed) { break }
            Write-Warning "时间戳 $ts 不可用，换下一个…"
        }
        if (-not $signed) { throw "签名失败: $f（时间戳服务器全部不可达？检查 AUTOMIND_TS_PROXY）" }
        & $signtool verify /pa $f
        if ($LASTEXITCODE -ne 0) { throw "签名后验证失败: $f" }
        Write-Host "已签名 ✓ $f" -ForegroundColor Green
    }
}
finally {
    if ($proxyChanged) {
        netsh winhttp reset proxy | Out-Null
        Write-Host "已还原 WinHTTP 直连"
    }
}
