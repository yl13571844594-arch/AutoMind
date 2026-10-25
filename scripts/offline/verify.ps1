<#
.SYNOPSIS
    AutoMind 离线验收自检脚本（Windows PowerShell 5.1+）。

.DESCRIPTION
    客户安全评审 / 交付验收时跑这一条命令，一次性核对：
      1. Python 环境与 AutoMind 安装版本（可选与 pyproject.toml 交叉校验）；
      2. 离线 / 白名单策略是否真的生效（调用 scripts\offline\smoke_offline.py）；
      3. net_check 三态结论（offline 下必须是 expected_offline 且跳过公网探测）；
      4. 出网审计日志断言：离线模式不存在公网放行记录、脱敏生效（无 query 泄漏）；
      5. 服务是否在监听预期端口（-SkipServiceCheck 可跳过）。

    只读：不写配置、不联网（-SkipNetwork 可进一步跳过所有探测）。
    退出码 0 = 验收通过；1 = 至少一项失败（输出里给出可执行的修复建议）。

.PARAMETER DataDir
    数据目录。默认沿用 AUTOMIND_DATA_DIR，其次 %APPDATA%\AutoMind，最后 .\.automind。

.PARAMETER ExpectedVersion
    期望的 AutoMind 版本（默认从 pyproject.toml 读取；读不到则不校验版本一致性）。

.PARAMETER ExpectMode
    期望的出网策略（offline / allowlist / open）。不传则以当前环境实际策略为准并只报告。

.PARAMETER EgressLog
    出网审计日志路径。默认 <数据目录>\logs\egress.log（也可从 smoke 输出里取）。

.PARAMETER SkipNetwork
    跳过网络探测（无外网调试、或不想在验收时产生任何探测流量时使用）。

.PARAMETER SkipServiceCheck
    跳过"服务端口是否在监听"检查（CLI / 未启动服务时使用）。

.PARAMETER Json
    以 JSON 输出验收结果（便于接入 CI 或交付报告）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\verify.ps1 -ExpectMode offline

.EXAMPLE
    powershell -NoProfile -File .\verify.ps1 -ExpectMode allowlist -SkipNetwork -Json
#>
[CmdletBinding()]
param(
    [string]$DataDir = "",
    [string]$ExpectedVersion = "",
    [ValidateSet("offline", "allowlist", "open")]
    [string]$ExpectMode = "",
    [string]$Python = "python",
    [int]$Port = 8765,
    [string]$EgressLog = "",
    [switch]$SkipNetwork,
    [switch]$SkipServiceCheck,
    [switch]$Json,
    [switch]$WhatIf,
    [Alias("dry-run")]
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$script:Dry = [bool]($WhatIf -or $DryRun)
$script:ScriptDir = $PSScriptRoot
$script:Checks = New-Object System.Collections.ArrayList

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}
function Write-Note([string]$Message) {
    Write-Host "    ..  $Message" -ForegroundColor DarkGray
}
function Write-WarnMsg([string]$Message) {
    Write-Host "    !!  $Message" -ForegroundColor Yellow
}
function Add-Check([string]$Name, [bool]$Ok, [string]$Detail) {
    [void]$script:Checks.Add([pscustomobject]@{ name = $Name; ok = $Ok; detail = [string]$Detail })
}
function Resolve-DataDir {
    $dir = $DataDir
    if (-not $dir) {
        if ($env:AUTOMIND_DATA_DIR) { $dir = $env:AUTOMIND_DATA_DIR }
        elseif ($env:APPDATA) { $dir = Join-Path $env:APPDATA "AutoMind" }
        else { $dir = Join-Path (Get-Location).Path ".automind" }
    }
    return [System.IO.Path]::GetFullPath($dir)
}
function Get-ListeningProcessId([int]$ListenPort) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction Stop |
            Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
        return 0
    }
    catch {
        $rows = & netstat -ano 2>$null | Select-String -Pattern (":" + [string]$ListenPort + "\s+.*LISTENING")
        foreach ($row in @($rows)) {
            $parts = (([string]$row).Trim()) -split "\s+"
            if ($parts.Count -ge 5) {
                $candidate = 0
                if ([int]::TryParse($parts[$parts.Count - 1], [ref]$candidate)) { return $candidate }
            }
        }
        return 0
    }
}

Write-Step "AutoMind 离线验收自检"
if ($script:Dry) {
    Write-WarnMsg "-WhatIf / -DryRun：只读检查照常执行，但跳过网络探测。"
    $SkipNetwork = $true
}

# ── 1. Python / 版本 ─────────────────────────────────────────
Write-Step "1/5 Python 与版本一致性"
$pyCommand = Get-Command $Python -ErrorAction SilentlyContinue
if (-not $pyCommand) {
    Add-Check "python" $false "找不到 Python（$Python）：请安装 Python 3.11+ 或用 -Python 指定完整路径。"
    $pyPath = ""
}
else {
    $pyPath = $pyCommand.Source
    $pyVersion = (& $pyPath -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1 | Out-String).Trim()
    $pyOk = ($LASTEXITCODE -eq 0) -and ([version]$pyVersion -ge [version]"3.11")
    Add-Check "python" $pyOk "Python $pyVersion ($pyPath)"
}
$installedVersion = ""
if ($pyPath) {
    $installedVersion = (& $pyPath -c "import automind; print(automind.__version__)" 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        Add-Check "installed_version" $false "无法 import automind：请先运行 install.ps1 或 pip install automind-agent。"
        $installedVersion = ""
    }
    else {
        Add-Check "installed_version" $true "automind.__version__=$installedVersion"
    }
}
$repoRoot = Split-Path -Parent (Split-Path -Parent $script:ScriptDir)
$repoPyproject = Join-Path $repoRoot "pyproject.toml"
$repoVersion = ""
if (Test-Path -LiteralPath $repoPyproject -PathType Leaf) {
    $m = [regex]::Match((Get-Content -LiteralPath $repoPyproject -Raw -Encoding UTF8),
        '(?m)^\s*version\s*=\s*"([^"]+)"')
    if ($m.Success) { $repoVersion = $m.Groups[1].Value }
}
if (-not $ExpectedVersion) { $ExpectedVersion = $repoVersion }
$versionOk = $true
$versionDetail = "未指定期望版本，跳过一致性校验（可用 -ExpectedVersion 指定）"
if ($ExpectedVersion -and $installedVersion) {
    $versionOk = ($installedVersion -eq $ExpectedVersion)
    $versionDetail = "期望 $ExpectedVersion，实际 $installedVersion"
    if ($repoVersion) { $versionDetail += "（pyproject.toml: $repoVersion）" }
    if ($repoVersion -and $repoVersion -ne $installedVersion) {
        $versionOk = $false
        $versionDetail += " —— 源码版本与已安装版本不一致，请先重新安装：pip install --no-index ..."
    }
}
Add-Check "version_consistency" $versionOk $versionDetail

# ── 2. 数据目录 ───────────────────────────────────────────────
Write-Step "2/5 数据目录"
$resolvedDataDir = Resolve-DataDir
if (Test-Path -LiteralPath $resolvedDataDir -PathType Container) {
    Add-Check "data_dir" $true $resolvedDataDir
}
else {
    Add-Check "data_dir" $false ("数据目录不存在：" + $resolvedDataDir + "。请先用 install.ps1 安装 / 启动一次服务生成数据目录。")
}

# ── 3. 冒烟自检（策略 / 三态 / 更新短路）───────────────────
Write-Step "3/5 策略与网络三态（smoke_offline.py）"
$smokeScript = Join-Path $script:ScriptDir "smoke_offline.py"
$smoke = $null
$auditPathFromSmoke = ""
if (-not (Test-Path -LiteralPath $smokeScript -PathType Leaf)) {
    Add-Check "smoke_selfcheck" $false "找不到 smoke_offline.py：$smokeScript（请完整拷贝 scripts\offline 目录）。"
}
elseif (-not $pyPath) {
    Add-Check "smoke_selfcheck" $false "Python 不可用，无法运行冒烟自检。"
}
else {
    $smokeArgs = @($smokeScript, "--json")
    if ($SkipNetwork) {
        $smokeArgs = @($smokeScript, "--json", "--skip-network")
    }
    $smokeRaw = (& $pyPath @smokeArgs 2>&1 | Out-String)
    $match = [regex]::Match($smokeRaw, "(?s)\{.*\}")
    if (-not $match.Success) {
        Add-Check "smoke_selfcheck" $false ("冒烟自检没有输出 JSON：" + $smokeRaw.Trim())
    }
    else {
        try {
            $smoke = $match.Value | ConvertFrom-Json
            Add-Check "smoke_selfcheck" ([bool]$smoke.ok) ("policy=" + $smoke.policy.mode +
                " network=" + $smoke.network.state + " updater_skipped=" + $smoke.updater.skipped)
            foreach ($check in $smoke.checks) {
                if (-not $check.ok) {
                    Add-Check ("smoke:" + $check.name) $false $check.detail
                }
            }
            if ($smoke.audit -and $smoke.audit.log_path) { $auditPathFromSmoke = [string]$smoke.audit.log_path }
        }
        catch {
            Add-Check "smoke_selfcheck" $false ("冒烟自检 JSON 解析失败：" + $_.Exception.Message)
        }
    }
}
$actualMode = ""
if ($smoke) { $actualMode = [string]$smoke.policy.mode }
if ($ExpectMode) {
    $modeOk = ($actualMode -eq $ExpectMode)
    Add-Check "policy_mode" $modeOk ("期望 " + $ExpectMode + "，实际 " + $(if ($actualMode) { $actualMode } else { "未知" }) + "；如不符请检查 AUTOMIND_NETWORK_POLICY / AUTOMIND_OFFLINE。")
}
else {
    Add-Check "policy_mode" $true ("未指定 -ExpectMode，当前策略：" + $(if ($actualMode) { $actualMode } else { "未知" }))
}
if ($smoke -and $SkipNetwork) {
    Add-Check "network_state" $true "已按 -SkipNetwork 跳过网络探测"
}
elseif ($smoke) {
    if ($actualMode -eq "offline") {
        Add-Check "network_state_offline" ($smoke.network.state -eq "expected_offline" -and $smoke.network.public_skipped) `
            ("offline 期望 expected_offline 且跳过公网探测，实际 " + $smoke.network.state + " / public_skipped=" + $smoke.network.public_skipped)
    }
    else {
        $stateOk = @("online", "degraded", "expected_offline") -contains [string]$smoke.network.state
        Add-Check "network_state" $stateOk ("state=" + $smoke.network.state + "；detail=" + $smoke.network.detail)
    }
}
if ($smoke) {
    $auditOk = [bool]$smoke.audit.enabled
    Add-Check "egress_audit_enabled" $auditOk ("audit_log=" + $smoke.audit.log_path + " enabled=" + $smoke.audit.enabled)
}

# ── 4. 出网审计日志断言 ──────────────────────────────────────
Write-Step "4/5 出网审计日志断言（无外联 / 脱敏）"
$auditPath = $EgressLog
if (-not $auditPath) { $auditPath = $auditPathFromSmoke }
if (-not $auditPath) { $auditPath = Join-Path (Join-Path $resolvedDataDir "logs") "egress.log" }
if (-not (Test-Path -LiteralPath $auditPath -PathType Leaf)) {
    Add-Check "egress_log" $true ("尚无出网审计记录（" + $auditPath + " 不存在）—— 未发生任何被审计的出站判定，符合离线验收预期。")
}
else {
    $lines = @(Get-Content -LiteralPath $auditPath -Encoding UTF8)
    $records = New-Object System.Collections.ArrayList
    $badJson = 0
    foreach ($line in $lines) {
        if (-not $line.Trim()) { continue }
        try {
            [void]$records.Add(($line | ConvertFrom-Json))
        }
        catch {
            $badJson++
        }
    }
    $deny = @($records | Where-Object { $_.event -eq "deny" })
    $allow = @($records | Where-Object { $_.event -eq "allow" })
    $suspicious = @()
    if ($actualMode -eq "offline") {
        # 离线模式只允许内网目标放行；出现 open_mode / allowlist 放行说明策略没生效
        $suspicious = @($allow | Where-Object { $_.reason -ne "intranet_target" })
    }
    elseif ($actualMode -eq "allowlist") {
        $suspicious = @($allow | Where-Object { $_.reason -eq "open_mode" })
    }
    $leak = @($lines | Where-Object { $_ -match '"target"\s*:\s*"[^"]*\?' })
    $auditOk = ($badJson -eq 0) -and ($suspicious.Count -eq 0) -and ($leak.Count -eq 0)
    $detail = ("共 " + $records.Count + " 条：allow=" + $allow.Count + " deny=" + $deny.Count +
        "；可疑公网放行=" + $suspicious.Count + "；query 泄漏=" + $leak.Count + "；坏行=" + $badJson)
    if ($suspicious.Count -gt 0) {
        $detail += "。离线 / 白名单模式下不应出现公网放行记录，请确认策略环境变量已生效并重启服务。"
    }
    if ($leak.Count -gt 0) {
        $detail += "。审计日志疑似记录了 URL query（脱敏失效），请升级到 v1.11.14+。"
    }
    Add-Check "egress_log" $auditOk $detail
}

# ── 5. 服务端口 ───────────────────────────────────────────────
Write-Step "5/5 服务进程与端口"
if ($SkipServiceCheck) {
    Add-Check "service_port" $true "已按 -SkipServiceCheck 跳过端口检查"
}
else {
    $pidValue = Get-ListeningProcessId -ListenPort $Port
    if ($pidValue -gt 0) {
        Add-Check "service_port" $true ("端口 " + $Port + " 正在监听（PID=" + $pidValue + "）")
    }
    else {
        Add-Check "service_port" $false ("端口 " + $Port + " 没有监听进程：服务可能未启动。启动命令：python -m automind.server --host 127.0.0.1 --port " + $Port + "（或加 -SkipServiceCheck 跳过本项）。")
    }
}

# ── 汇总 ─────────────────────────────────────────────────────
$failed = @($script:Checks | Where-Object { -not $_.ok })
$ok = ($failed.Count -eq 0)
$result = [pscustomobject]@{
    ok          = $ok
    version     = $installedVersion
    mode        = $actualMode
    data_dir    = $resolvedDataDir
    checks      = @($script:Checks)
    failed      = $failed.Count
    generated_at = (Get-Date).ToString("o")
}
if ($Json) {
    $result | ConvertTo-Json -Depth 6
}
else {
    Write-Host ""
    Write-Host "验收结果：" -ForegroundColor Cyan
    foreach ($check in $script:Checks) {
        if ($check.ok) {
            Write-Host ("  [PASS] " + $check.name) -ForegroundColor Green
        }
        else {
            Write-Host ("  [FAIL] " + $check.name) -ForegroundColor Red
        }
        Write-Host ("         " + $check.detail) -ForegroundColor DarkGray
    }
    Write-Host ""
    if ($ok) {
        Write-Host "结论：离线验收通过（$($script:Checks.Count) 项检查）。" -ForegroundColor Green
    }
    else {
        Write-Host "结论：离线验收未通过（$($failed.Count) 项失败）。请按上面的提示逐项修复后重跑。" -ForegroundColor Red
    }
}
if ($ok) { exit 0 } else { exit 1 }
