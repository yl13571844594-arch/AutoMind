<#
.SYNOPSIS
    AutoMind 离线安装脚本（Windows PowerShell 5.1+）。

.DESCRIPTION
    在**没有外网**的 Windows 机器上，从离线包（wheel 目录或单个 .whl）安装
    AutoMind，写入离线 / 白名单策略配置，并可选注册开机自启。全程不发任何
    外网请求（pip 使用 --no-index）。

    幂等：已安装相同版本且配置一致时直接跳过 pip；-Force 可强制重装。
    失败回滚：安装前备份当前 automind 包目录；pip 或验收失败时自动恢复。

    离线包准备（在一台能上网的机器上执行，然后把整个目录拷进来）：
        pip download "automind-agent[web]==1.11.14" -d .\offline-wheels
        # 把 offline-wheels 目录与 scripts\offline 一起拷到内网机器

.PARAMETER Package
    离线包路径：wheel 目录（推荐，含全部依赖）或单个 .whl 文件。

.PARAMETER DataDir
    数据目录（配置 / 数据库 / 日志）。默认沿用 AUTOMIND_DATA_DIR 或 %APPDATA%\AutoMind。

.PARAMETER Mode
    出网策略：offline（默认，禁止公网出网）/ allowlist（仅白名单）/ open（不限制）。

.PARAMETER EgressAllow
    出网白名单文件（每行一条，支持 *.example.com 与 host:port）。allowlist 模式建议提供。

.PARAMETER RegisterAutostart
    注册当前用户开机自启（HKCU Run 键，不写系统服务，避免要求管理员）。

.PARAMETER ProbeHosts
    可选：AUTOMIND_PROBE_HOSTS，逗号分隔 host:port（内网部署请指向可达的诊断目标）。

.PARAMETER WhatIf / -DryRun
    只打印将要执行的动作，不写文件、不 pip install、不改环境变量。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 `
        -Package .\offline-wheels -DataDir D:\AutoMind -Mode offline -RegisterAutostart

.EXAMPLE
    powershell -NoProfile -File .\install.ps1 -Package .\offline-wheels -Mode allowlist `
        -EgressAllow .\egress-allow.txt -WhatIf
#>
[CmdletBinding()]
param(
    [string]$Package = "",
    [string]$DataDir = "",
    [ValidateSet("offline", "allowlist", "open")]
    [string]$Mode = "offline",
    [string]$EgressAllow = "",
    [string]$Python = "python",
    [string]$ExpectedVersion = "1.11.14",
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8765,
    [string]$ProbeHosts = "",
    [switch]$RegisterAutostart,
    [switch]$RemoveAutostart,
    [switch]$NoPersistEnv,
    [switch]$Force,
    [switch]$WhatIf,
    [Alias("dry-run")]
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$script:Dry = [bool]($WhatIf -or $DryRun)
$script:ScriptDir = $PSScriptRoot
$script:RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$script:RunValue = "AutoMind"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}
function Write-Ok([string]$Message) {
    Write-Host "    OK  $Message" -ForegroundColor Green
}
function Write-Note([string]$Message) {
    Write-Host "    ..  $Message" -ForegroundColor DarkGray
}
function Write-WarnMsg([string]$Message) {
    Write-Host "    !!  $Message" -ForegroundColor Yellow
}
function Fail([string]$Message) {
    Write-Host ""
    Write-Host "[失败] $Message" -ForegroundColor Red
    Write-Host "        未完成的步骤可修复后重跑本脚本（幂等）；数据目录未被删除。" -ForegroundColor Red
    exit 1
}
function Invoke-DryAware([string]$Description, [scriptblock]$Action) {
    if ($script:Dry) {
        Write-Host "    [DRY-RUN] $Description" -ForegroundColor DarkGray
        return
    }
    Write-Host "    -> $Description"
    & $Action
}

Write-Step "AutoMind 离线安装 / 配置（v$ExpectedVersion）"
if ($script:Dry) {
    Write-WarnMsg "DRY-RUN 模式：只打印计划，不安装、不写文件、不改环境变量。"
}
Write-Note "Python      : $Python"
Write-Note "Package     : $Package"
Write-Note "DataDir     : $(if ($DataDir) { $DataDir } else { '(自动解析)' })"
Write-Note "Mode        : $Mode"
Write-Note "Autostart   : $([bool]$RegisterAutostart)"

if ($Package -and -not (Test-Path -LiteralPath $Package)) {
    Fail "-Package 指向的路径不存在：$Package。请把离线 wheel 目录（或 .whl 文件）拷到本机后重新执行。"
}
if ($EgressAllow -and -not (Test-Path -LiteralPath $EgressAllow)) {
    Fail "-EgressAllow 指向的白名单文件不存在：$EgressAllow"
}

# ── 1. 解析 Python ─────────────────────────────────────────────
Write-Step "1/7 检查 Python 环境"
$pyCommand = Get-Command $Python -ErrorAction SilentlyContinue
if (-not $pyCommand) {
    Fail "找不到 Python（$Python）。请安装 Python 3.11+，或用 -Python 指定完整路径（例如 -Python C:\Python312\python.exe）。"
}
$pyPath = $pyCommand.Source
$pyVersion = (& $pyPath -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    Fail "无法运行 $pyPath --version，请检查 Python 安装是否完整。输出：$pyVersion"
}
if ([version]$pyVersion -lt [version]"3.11") {
    Fail "Python 版本过低（当前 $pyVersion，需要 3.11+）。"
}
Write-Ok "Python $pyVersion ($pyPath)"

$currentVersion = ""
$sitePackageDir = ""
$probeOut = & $pyPath -c "import automind, os; print(automind.__version__); print(os.path.dirname(automind.__file__))" 2>$null
if ($LASTEXITCODE -eq 0 -and $probeOut) {
    $lines = @($probeOut)
    $currentVersion = ([string]($lines[0])).Trim()
    if ($lines.Count -gt 1) { $sitePackageDir = ([string]($lines[1])).Trim() }
    Write-Ok "已安装 AutoMind $currentVersion"
}
else {
    Write-Note "当前环境未安装 AutoMind（将全新安装）"
}

# ── 2. 解析数据目录与离线包 ────────────────────────────────────
Write-Step "2/7 解析数据目录与离线包"
if (-not $DataDir) {
    if ($env:AUTOMIND_DATA_DIR) {
        $DataDir = $env:AUTOMIND_DATA_DIR
    }
    elseif ($env:APPDATA) {
        $DataDir = Join-Path $env:APPDATA "AutoMind"
    }
    else {
        $DataDir = Join-Path (Get-Location).Path ".automind"
    }
}
$DataDir = [System.IO.Path]::GetFullPath($DataDir)
Write-Note "数据目录：$DataDir"

$whlDir = ""
$singleWheel = ""
if ($Package) {
    $pkgPath = (Resolve-Path -LiteralPath $Package).Path
    if ($pkgPath -match "\.whl$") {
        $singleWheel = $pkgPath
        $whlDir = Split-Path -Parent $pkgPath
    }
    else {
        $whlDir = $pkgPath
    }
    $wheels = @(Get-ChildItem -LiteralPath $whlDir -Filter "*.whl" -ErrorAction SilentlyContinue)
    if ($wheels.Count -eq 0) {
        Fail "离线包目录里没有 .whl 文件：$whlDir。请在能上网的机器上执行 pip download 后整目录拷贝。"
    }
    Write-Ok "离线包目录：$whlDir（$($wheels.Count) 个 whl）"
}
else {
    Write-Note "未指定 -Package：假设 AutoMind 已安装，仅写入配置 / 环境变量 / 自启。"
}

$versionMatches = ($currentVersion -eq $ExpectedVersion)
$needInstall = (-not $versionMatches) -or $Force
if ($versionMatches -and -not $Force) {
    Write-Note "版本已一致（$currentVersion），跳过 pip 安装（-Force 可强制重装）。"
}

# ── 3. 备份当前安装 + pip 离线安装 ─────────────────────────────
Write-Step "3/7 离线安装（pip --no-index）"
$installBackup = ""
if ($needInstall) {
    if (-not $Package) {
        Fail "需要安装但未提供 -Package。请指定离线 wheel 目录或单个 .whl。"
    }
    if ($sitePackageDir -and (Test-Path -LiteralPath $sitePackageDir)) {
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $installBackup = Join-Path $DataDir ("logs\install-backup\automind-" + $currentVersion + "-" + $stamp)
        Invoke-DryAware "备份当前安装到 $installBackup" {
            New-Item -ItemType Directory -Path (Split-Path -Parent $installBackup) -Force | Out-Null
            Copy-Item -LiteralPath $sitePackageDir -Destination $installBackup -Recurse -Force
        }
    }
    $pipArgs = @("-m", "pip", "install", "--no-index", "--find-links", $whlDir, "--disable-pip-version-check")
    if ($singleWheel) {
        $pipArgs += $singleWheel
    }
    else {
        $pipArgs += ("automind-agent[web]==" + $ExpectedVersion)
    }
    $pipOutput = ""
    Invoke-DryAware ("pip " + ($pipArgs -join " ")) {
        $script:pipOutput = (& $pyPath @pipArgs 2>&1 | Out-String)
        $script:pipExit = $LASTEXITCODE
    }
    if (-not $script:Dry) {
        if ($script:pipExit -ne 0) {
            Write-Host $script:pipOutput -ForegroundColor DarkGray
            if ($installBackup -and (Test-Path -LiteralPath $installBackup)) {
                Write-WarnMsg "pip 安装失败，正在恢复安装前的包目录……"
                $restoreTarget = & $pyPath -c "import automind, os; print(os.path.dirname(automind.__file__))" 2>$null
                if ($LASTEXITCODE -eq 0 -and $restoreTarget) {
                    Remove-Item -LiteralPath $restoreTarget -Recurse -Force -ErrorAction SilentlyContinue
                    Copy-Item -LiteralPath $installBackup -Destination $restoreTarget -Recurse -Force
                }
            }
            Fail "pip 离线安装失败（见上方输出）。常见原因：离线包目录缺少依赖 wheel（如 fastapi/uvicorn/httpx）；请在有网机器上 pip download 'automind-agent[web]==$ExpectedVersion' 后重新拷贝整个目录。"
        }
        Write-Ok "pip 安装完成"
    }
}
else {
    Write-Note "跳过 pip 安装（DRY-RUN 或版本已一致）"
}

# ── 4. 安装后版本校验（不通过则回滚）─────────────────────────
Write-Step "4/7 校验安装版本"
if (-not $script:Dry) {
    $installed = (& $pyPath -c "import automind; print(automind.__version__)" 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        Fail "安装后无法 import automind：$installed"
    }
    if ($installed -ne $ExpectedVersion) {
        if ($installBackup -and (Test-Path -LiteralPath $installBackup)) {
            Write-WarnMsg "版本不一致（期望 $ExpectedVersion，实际 $installed），正在回滚安装前的包目录……"
            $restoreTarget = & $pyPath -c "import automind, os; print(os.path.dirname(automind.__file__))" 2>$null
            if ($LASTEXITCODE -eq 0 -and $restoreTarget) {
                Remove-Item -LiteralPath $restoreTarget -Recurse -Force -ErrorAction SilentlyContinue
                Copy-Item -LiteralPath $installBackup -Destination $restoreTarget -Recurse -Force
            }
        }
        Fail "安装后版本不一致：期望 $ExpectedVersion，实际 $installed。已尝试回滚；请核对离线包版本。"
    }
    Write-Ok "版本校验通过：$installed"
}
else {
    Write-Note "[DRY-RUN] 将校验 automind.__version__ == $ExpectedVersion"
}

# ── 5. 写策略配置 + 白名单 ─────────────────────────────────────
Write-Step "5/7 写入离线策略配置"
$configDir = Join-Path $DataDir "config"
$configFile = Join-Path $configDir "offline-config.json"
$allowFile = Join-Path $configDir "egress-allow.txt"
Invoke-DryAware "创建配置目录 $configDir" {
    New-Item -ItemType Directory -Path $configDir -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $DataDir "logs") -Force | Out-Null
}
if ($EgressAllow) {
    Invoke-DryAware "复制白名单到 $allowFile" {
        Copy-Item -LiteralPath $EgressAllow -Destination $allowFile -Force
    }
}
elseif ($Mode -eq "allowlist") {
    if (-not (Test-Path -LiteralPath $allowFile)) {
        Invoke-DryAware "生成白名单模板 $allowFile（默认空白 = 拒绝所有公网出网）" {
            $template = @"
# AutoMind 出网白名单（每行一条，# 之后为注释）
# 支持 host、host:port、*.example.com 通配、IP / CIDR。
# 示例（按需取消注释）：
# api.deepseek.com
# open.feishu.cn
# *.corp.example.com:8443
"@
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText($allowFile, $template, $utf8NoBom)
        }
    }
    Write-WarnMsg "allowlist 模式：白名单为空时所有公网出网都会被拒绝（fail-closed）。请按需编辑 $allowFile 后重启服务。"
}
$config = [ordered]@{
    version           = $ExpectedVersion
    mode              = $Mode
    egress_allow_file = $allowFile
    data_dir          = $DataDir
    host              = $HostName
    port              = $Port
    installed_at      = (Get-Date).ToString("o")
}
$configJson = ($config | ConvertTo-Json -Depth 5)
Invoke-DryAware "写入 $configFile" {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($configFile, $configJson, $utf8NoBom)
}

# ── 6. 环境变量（当前进程 + 用户级持久化）────────────────────
Write-Step "6/7 配置环境变量（用户级，重启终端/应用后生效）"
$envMap = [ordered]@{
    AUTOMIND_NETWORK_POLICY   = $Mode
    AUTOMIND_EGRESS_ALLOW_FILE = $allowFile
    AUTOMIND_DATA_DIR         = $DataDir
    AUTOMIND_EGRESS_AUDIT     = "1"
}
if ($Mode -eq "offline") { $envMap["AUTOMIND_OFFLINE"] = "1" }
if ($ProbeHosts) { $envMap["AUTOMIND_PROBE_HOSTS"] = $ProbeHosts }
foreach ($key in $envMap.Keys) {
    $value = [string]$envMap[$key]
    if ($NoPersistEnv) {
        Write-Note "[跳过持久化] $key=$value"
    }
    else {
        Invoke-DryAware "设置用户环境变量 $key=$value" {
            [Environment]::SetEnvironmentVariable($key, $value, "User")
        }
    }
    # 当前进程也设上，后面的冒烟自检才能看到同样的策略（DRY-RUN 不改环境）
    if (-not $script:Dry) {
        Set-Item -Path ("Env:" + $key) -Value $value
    }
}
if ($Mode -ne "offline" -and -not $NoPersistEnv) {
    Invoke-DryAware "清除用户环境变量 AUTOMIND_OFFLINE（非离线模式）" {
        [Environment]::SetEnvironmentVariable("AUTOMIND_OFFLINE", $null, "User")
    }
    Remove-Item Env:\AUTOMIND_OFFLINE -ErrorAction SilentlyContinue
}

# ── 7. 开机自启 / 冒烟自检 ─────────────────────────────────────
Write-Step "7/7 开机自启与冒烟自检"
if ($RegisterAutostart -or $RemoveAutostart) {
    $pythonw = Join-Path (Split-Path -Parent $pyPath) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $pyPath }
    $launchCmd = '"' + $pythonw + '" -m automind.server --host ' + $HostName + ' --port ' + [string]$Port
    if ($RemoveAutostart) {
        Invoke-DryAware "删除开机自启项 HKCU:\...\Run\$script:RunValue" {
            Remove-ItemProperty -Path $script:RunKey -Name $script:RunValue -ErrorAction SilentlyContinue
        }
    }
    else {
        Invoke-DryAware "注册开机自启：$launchCmd" {
            if (-not (Test-Path -LiteralPath $script:RunKey)) {
                New-Item -Path $script:RunKey -Force | Out-Null
            }
            Set-ItemProperty -Path $script:RunKey -Name $script:RunValue -Value $launchCmd
        }
    }
}
else {
    Write-Note "未指定 -RegisterAutostart，跳过自启配置（可用 -RegisterAutostart 开启）"
}

$smokeScript = Join-Path $script:ScriptDir "smoke_offline.py"
if ((Test-Path -LiteralPath $smokeScript) -and -not $script:Dry) {
    Write-Note "运行冒烟自检：python smoke_offline.py --json"
    $smokeRaw = (& $pyPath $smokeScript --json 2>&1 | Out-String)
    $smokeText = $smokeRaw.Trim()
    $match = [regex]::Match($smokeText, "(?s)\{.*\}")
    if (-not $match.Success) {
        Fail "冒烟自检没有输出 JSON，输出如下：$smokeText"
    }
    try {
        $smoke = $match.Value | ConvertFrom-Json
    }
    catch {
        Fail "冒烟自检输出无法解析为 JSON：$smokeText"
    }
    if (-not $smoke.ok) {
        foreach ($check in $smoke.checks) {
            if (-not $check.ok) { Write-WarnMsg ("自检失败：" + $check.name + " — " + $check.detail) }
        }
        Fail "冒烟自检未通过（见上方）。修复后重跑本脚本。"
    }
    if ($smoke.policy.mode -ne $Mode) {
        Fail "冒烟自检策略不一致：期望 $Mode，实际 $($smoke.policy.mode)。"
    }
    Write-Ok "冒烟自检通过：policy=$($smoke.policy.mode)，network=$($smoke.network.state)"
}
else {
    Write-Note "[DRY-RUN] 将运行 smoke_offline.py --json 做安装后冒烟自检"
}

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
$finalVersion = $currentVersion
if (-not $script:Dry) {
    $finalVersion = (& $pyPath -c "import automind; print(automind.__version__)" 2>$null | Out-String).Trim()
}
Write-Host "  版本    : $(if ($finalVersion) { $finalVersion } else { $ExpectedVersion })"
Write-Host "  数据目录: $DataDir"
Write-Host "  策略    : $Mode"
Write-Host "  启动    : `"$pyPath`" -m automind.server --host $HostName --port $Port"
Write-Host "  验收    : powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $script:ScriptDir 'verify.ps1')`""
Write-Host "  升级前  : 先执行 backup.ps1；回滚用 rollback.ps1（详见 docs\OFFLINE_OPS.md）"
exit 0
