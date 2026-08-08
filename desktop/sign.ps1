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
    foreach ($root in @("$env:LOCALAPPDATA\AutoMindBuildTools",
                        "${env:ProgramFiles(x86)}\Windows Kits\10\bin",
                        "$env:ProgramFiles\Windows Kits\10\bin")) {
        if (-not (Test-Path $root)) { continue }
        $all = @(Get-ChildItem $root -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue)
        if ($all.Count -eq 0) { continue }
        # 优先 x64（Windows Kits 按架构分目录：10.0.x\x64\signtool.exe），
        # 但**没有 x64 目录时不能直接放弃** —— 从 NuGet 抽出来固定在
        # AutoMindBuildTools\bin\ 的那份路径里就不含 "x64"，此前被这条过滤
        # 一并排除，导致"signtool 明明在却报找不到"，进而产出未签名包。
        $x64 = @($all | Where-Object { $_.FullName -like "*x64*" })
        $pick = if ($x64.Count -gt 0) { $x64 } else { $all }
        return ($pick | Sort-Object FullName -Descending | Select-Object -First 1).FullName
    }
    return $null   # 交由调用方回退到 Set-AuthenticodeSignature
}

# ── signtool 缺席时的回退：PowerShell 内建 Authenticode ────────────────
# 本机没装 Windows SDK 时（常见：只做 Python 打包的开发机），signtool 根本不
# 存在。此前脚本直接抛错中断，等于"证书明明可用却签不了"。
# Set-AuthenticodeSignature 是 PowerShell 自带的，走同一套 CryptoAPI，
# 对 Certum SimplySign 这类云证书同样有效（私钥留在云端 HSM，PIN 由
# SimplySign Desktop 会话处理，本脚本依旧从不经手）。
# 实测：签 AutoMind.exe 返回 Status=Valid 且带 RFC3161 时间戳。
# 差异：signtool 支持双签名(SHA1+SHA256)与更细的 /ph 页哈希控制，
# 回退路径只做 SHA256 单签 —— 对 Win10+ 完全够用。
# 注意：**不会**去用系统里搜到的任何第三方 signtool（曾在某银行客户端目录下
# 发现一个来路不明的同名程序）—— 生产签名只用官方 SDK 版或本回退。
function Invoke-PsSign($file, $cert, $tsList) {
    foreach ($ts in $tsList) {
        $r = Set-AuthenticodeSignature -FilePath $file -Certificate $cert `
            -HashAlgorithm SHA256 -TimestampServer $ts -ErrorAction SilentlyContinue
        if ($r -and $r.Status -eq "Valid") {
            Write-Host "  已签名（时间戳: $ts）"
            return $true
        }
        Write-Warning "  时间戳 $ts 失败：$($r.StatusMessage)"
    }
    return $false
}

$signtool = Find-SignTool
if ($signtool) {
    Write-Host "signtool: $signtool"
} else {
    Write-Host "未找到 signtool.exe —— 回退到 PowerShell 内建 Authenticode 签名"
}

if ($VerifyOnly) {
    foreach ($f in $Path) {
        if ($signtool) {
            & $signtool verify /pa /v $f
            if ($LASTEXITCODE -ne 0) { throw "签名验证失败: $f" }
        } else {
            $s = Get-AuthenticodeSignature -FilePath $f
            Write-Host ("$f → " + $s.Status + " | " + $s.SignerCertificate.Subject)
            if ($s.Status -ne "Valid") { throw "签名验证失败: $f（$($s.Status)）" }
        }
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

# ── WinINET(IE) 代理：真正决定 signtool 能否打时间戳的那一个 ──────────
# 踩坑记录（2026-07-29 实测定位）：signtool 打时间戳走 WinHTTP，但它**同时会
# 采用 WinINET/IE 的每用户代理设置** —— 即使 `netsh winhttp show proxy` 显示
# "Direct access"，只要 IE 代理开着（HKCU\...\Internet Settings\ProxyEnable=1），
# 请求依旧被送进那个代理。本机代理对 sectigo/certum/digicert 的 RFC3161 POST
# 会断连或超时，于是四个时间戳服务器**全部失败**，报「could not be reached」，
# 让人误以为是网络或 AUTOMIND_TS_PROXY 配错 —— 实际两者都不是。
# 对照实验：同一时刻 Python 直连 POST 四个 TSA 全部返回合法 timestamp-reply；
# 把 ProxyEnable 临时置 0 后 signtool 一次成功。
# 故此处兜底：全部 TSA 失败且 IE 代理开着时，临时关掉它整轮重试，再还原。
$ieReg = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
function Get-IEProxyEnabled {
    try { return [int](Get-ItemProperty -Path $ieReg -ErrorAction Stop).ProxyEnable } catch { return 0 }
}
function Set-IEProxyEnabled([int]$v) {
    try { Set-ItemProperty -Path $ieReg -Name ProxyEnable -Value $v -ErrorAction Stop } catch {}
}

function Invoke-SignAttempts($file) {
    # 回退路径：没有 signtool 时用内建 Authenticode（证书对象取自证书库指纹）
    if (-not $signtool) {
        if (-not $thumb) {
            throw "回退签名仅支持证书库指纹（AUTOMIND_CERT_THUMBPRINT）；PFX 请安装 Windows SDK 后用 signtool"
        }
        $cert = Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -CodeSigningCert `
            -ErrorAction SilentlyContinue | Where-Object { $_.Thumbprint -eq $thumb } | Select-Object -First 1
        if (-not $cert) { throw "证书库中找不到指纹为 $thumb 的代码签名证书" }
        return (Invoke-PsSign $file $cert $tsServers)
    }
    foreach ($ts in $tsServers) {
        foreach ($try in 1..2) {
            Write-Host "签名 $file（时间戳: $ts，第 $try 次）…"
            # 必须把 signtool 的输出送去 Out-Host：PowerShell 函数会把**所有**未捕获
            # 的输出当作返回值，signtool 那几行 stdout 会混进返回的数组里，让调用方
            # 拿到一个非空数组（永远为真）而误判成签名成功。
            if ($thumb) {
                & $signtool sign /fd sha256 /td sha256 /tr $ts /sha1 $thumb $file | Out-Host
            } else {
                & $signtool sign /fd sha256 /td sha256 /tr $ts /f $pfx /p $env:AUTOMIND_CERT_PWD $file | Out-Host
            }
            if ($LASTEXITCODE -eq 0) { return $true }
        }
        Write-Warning "时间戳 $ts 不可用，换下一个…"
    }
    return $false
}

$ieProxyOrig = $null
try {
    foreach ($f in $Path) {
        if (-not (Test-Path $f)) { throw "文件不存在: $f" }
        $signed = Invoke-SignAttempts $f
        if (-not $signed -and (Get-IEProxyEnabled) -eq 1) {
            Write-Warning "四个时间戳服务器全部失败，且检测到 IE 代理开启 —— 临时关闭后整轮重试"
            if ($null -eq $ieProxyOrig) { $ieProxyOrig = 1 }
            Set-IEProxyEnabled 0
            $signed = Invoke-SignAttempts $f
        }
        if (-not $signed) {
            throw ("签名失败: $f —— 时间戳全部不可达。排查：`n" +
                   "  1) 用 RFC3161 POST 直连测四个 TSA，确认网络本身通不通；`n" +
                   "  2) 检查 IE 代理 (HKCU\\...\\Internet Settings\\ProxyEnable)，" +
                   "signtool 会采用它，而非只看 netsh winhttp；`n" +
                   "  3) 确认 SimplySign Desktop 会话未过期（私钥不可用也会签不上）")
        }
        # 签完立刻复验：signtool 与回退路径各用各的校验方式
        if ($signtool) {
            & $signtool verify /pa $f
            if ($LASTEXITCODE -ne 0) { throw "签名后验证失败: $f" }
        } else {
            $vs = Get-AuthenticodeSignature -FilePath $f
            if ($vs.Status -ne "Valid") { throw "签名后验证失败: $f（$($vs.Status)）" }
        }
        Write-Host "已签名 ✓ $f" -ForegroundColor Green
    }
}
finally {
    if ($proxyChanged) {
        netsh winhttp reset proxy | Out-Null
        Write-Host "已还原 WinHTTP 直连"
    }
    if ($null -ne $ieProxyOrig) {
        Set-IEProxyEnabled $ieProxyOrig
        Write-Host "已还原 IE 代理开关 = $ieProxyOrig"
    }
}
