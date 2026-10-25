<#
.SYNOPSIS
    AutoMind 数据回滚脚本（Windows PowerShell 5.1+）。

.DESCRIPTION
    按 backup.ps1 生成的备份包恢复数据目录。执行顺序刻意做成"怎么都不会把当前
    状态弄丢"：

      1. 校验备份包完整性（有 .sha256 清单时比对 SHA-256）；
      2. 停止占用数据目录的服务（按端口找监听进程，或 -SkipStop 跳过）；
      3. **先备份当前状态**（自动调用 backup.ps1 生成 pre-rollback 备份）；
      4. 解压到临时目录 → 原子替换（旧目录先改名保留，新目录再改名就位）；
      5. 任何一步失败 → 把旧目录改回来，并明确报告失败点。

.PARAMETER Backup
    备份 zip 路径（backup.ps1 产物）。

.PARAMETER DataDir
    要恢复到的数据目录。默认沿用 AUTOMIND_DATA_DIR 或 %APPDATA%\AutoMind。

.PARAMETER Port
    用于停止服务的监听端口（默认 8765）。找不到监听进程时不报错，只提示。

.PARAMETER Yes
    跳过交互确认（无人值守脚本必加）。

.PARAMETER SkipStop
    不停止服务（确认服务已停时使用）。

.PARAMETER WhatIf / -DryRun
    只打印将要执行的动作，不停止进程、不写文件、不改目录。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\rollback.ps1 -Backup D:\backups\automind-backup-20261021-120000.zip -Yes

.EXAMPLE
    powershell -NoProfile -File .\rollback.ps1 -Backup .\automind-backup-20261021-120000.zip -WhatIf
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Backup,
    [string]$DataDir = "",
    [int]$Port = 8765,
    [switch]$Yes,
    [switch]$SkipStop,
    [switch]$WhatIf,
    [Alias("dry-run")]
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$script:Dry = [bool]($WhatIf -or $DryRun)
$script:ScriptDir = $PSScriptRoot

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
    exit 1
}

function Get-ListeningProcessId([int]$ListenPort) {
    # 按端口找监听进程 PID；找不到返回 0。
    try {
        $conn = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction Stop |
            Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
        return 0
    }
    catch {
        # 老系统 / 无 Get-NetTCPConnection：退回解析 netstat
        $rows = & netstat -ano 2>$null | Select-String -Pattern (":" + [string]$ListenPort + "\s+.*LISTENING")
        foreach ($row in @($rows)) {
            $text = ([string]$row).Trim()
            $parts = $text -split "\s+"
            if ($parts.Count -ge 5) {
                $candidate = 0
                if ([int]::TryParse($parts[$parts.Count - 1], [ref]$candidate)) { return $candidate }
            }
        }
        return 0
    }
}

Write-Step "AutoMind 数据回滚"
if ($script:Dry) {
    Write-WarnMsg "DRY-RUN 模式：只打印计划，不停止进程、不写文件。"
}

# ── 1. 校验备份包 ─────────────────────────────────────────────
Write-Step "1/5 校验备份包"
if (-not (Test-Path -LiteralPath $Backup -PathType Leaf)) {
    Fail "备份文件不存在：$Backup。请用 backup.ps1 的产物路径（-Backup）。"
}
$Backup = (Resolve-Path -LiteralPath $Backup).Path
if ($Backup -notmatch "\.zip$") {
    Fail "备份文件必须是 .zip：$Backup"
}
$shaFile = $Backup + ".sha256"
if (Test-Path -LiteralPath $shaFile -PathType Leaf) {
    $expected = ((Get-Content -LiteralPath $shaFile -Raw -Encoding UTF8) -split "\s+")[0].Trim().ToLowerInvariant()
    $actual = (Get-FileHash -LiteralPath $Backup -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($expected -and $expected -ne $actual) {
        Fail "备份包 SHA-256 校验失败：期望 $expected，实际 $actual。备份包可能损坏或被篡改，请换一个备份。"
    }
    Write-Ok "SHA-256 校验通过：$actual"
}
else {
    Write-WarnMsg "备份包没有 .sha256 清单，无法校验完整性（继续执行，风险自担）。"
}

# ── 2. 解析数据目录 ───────────────────────────────────────────
Write-Step "2/5 解析数据目录"
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
$parent = Split-Path -Parent $DataDir
if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    Fail "数据目录的上级目录不存在：$parent。请用 -DataDir 指定正确路径。"
}
Write-Note "恢复目标：$DataDir"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("automind-rollback-" + $stamp)
$oldDir = $DataDir + ".before-rollback-" + $stamp

# ── 3. 确认 ───────────────────────────────────────────────────
if (-not $Yes -and -not $script:Dry) {
    Write-WarnMsg "即将用备份覆盖数据目录：$DataDir"
    $answer = Read-Host "确认请输入 yes"
    if ($answer -ne "yes" -and $answer -ne "y") {
        Write-Host "已取消（未做任何修改）。" -ForegroundColor Yellow
        exit 1
    }
}

# ── 4. 停服务 + 备份当前状态 ─────────────────────────────────
Write-Step "3/5 停止服务并备份当前状态"
if (-not $SkipStop) {
    $pidValue = Get-ListeningProcessId -ListenPort $Port
    if ($pidValue -gt 0) {
        if ($script:Dry) {
            Write-Host "    [DRY-RUN] 将停止监听端口 $Port 的进程 PID=$pidValue" -ForegroundColor DarkGray
        }
        else {
            Write-Note "停止监听端口 $Port 的进程 PID=$pidValue"
            Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            $still = Get-ListeningProcessId -ListenPort $Port
            if ($still -gt 0) {
                Fail "进程 PID=$still 仍占用端口 $Port，无法安全替换数据目录。请手动停止 AutoMind 后重试（或加 -SkipStop 前先自行停止）。"
            }
        }
    }
    else {
        Write-Note "端口 $Port 上没有监听进程（服务可能已停止）"
    }
}
else {
    Write-Note "-SkipStop：跳过停止服务"
}

$preBackupDir = Join-Path $DataDir "backups"
if (-not (Test-Path -LiteralPath $DataDir -PathType Container)) {
    Write-WarnMsg "数据目录当前不存在（$DataDir），跳过 pre-rollback 备份。"
}
else {
    $backupScript = Join-Path $script:ScriptDir "backup.ps1"
    if (Test-Path -LiteralPath $backupScript -PathType Leaf) {
        if ($script:Dry) {
            Write-Host "    [DRY-RUN] 将调用 backup.ps1 生成 pre-rollback 备份" -ForegroundColor DarkGray
        }
        else {
            Write-Note "调用 backup.ps1 生成 pre-rollback 备份"
            & $backupScript -DataDir $DataDir -OutDir $preBackupDir
            if ($LASTEXITCODE -ne 0) {
                Fail "pre-rollback 备份失败（见上方输出），已中止回滚：当前数据未被修改。"
            }
        }
    }
    else {
        Write-WarnMsg "找不到 backup.ps1，跳过 pre-rollback 备份（建议先用 backup.ps1 手动备份当前状态）。"
    }
}

# ── 5. 解压 + 原子替换 ───────────────────────────────────────
Write-Step "4/5 解压并原子替换"
Add-Type -AssemblyName System.IO.Compression.FileSystem
if ($script:Dry) {
    Write-Host "    [DRY-RUN] 将解压 $Backup 到 $staging，然后：" -ForegroundColor DarkGray
    Write-Host "              $DataDir -> $oldDir" -ForegroundColor DarkGray
    Write-Host "              $staging\data -> $DataDir" -ForegroundColor DarkGray
}
else {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    try {
        [System.IO.Compression.ZipFile]::ExtractToDirectory($Backup, $staging)
    }
    catch {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
        Fail "解压备份失败：$($_.Exception.Message)。备份包可能损坏，请换一个备份。"
    }
    $payload = Join-Path $staging "data"
    if (-not (Test-Path -LiteralPath $payload -PathType Container)) {
        $payload = $staging          # 兼容手工制作的备份（直接就是数据目录内容）
    }

    $movedOld = $false
    try {
        if (Test-Path -LiteralPath $DataDir -PathType Container) {
            Move-Item -LiteralPath $DataDir -Destination $oldDir -Force
            $movedOld = $true
        }
        if ([System.IO.Path]::GetFullPath($payload) -ne [System.IO.Path]::GetFullPath($staging)) {
            Move-Item -LiteralPath $payload -Destination $DataDir -Force
        }
        else {
            New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
            foreach ($item in @(Get-ChildItem -LiteralPath $staging -Force)) {
                Move-Item -LiteralPath $item.FullName -Destination $DataDir -Force
            }
        }
    }
    catch {
        Write-WarnMsg "替换数据目录失败：$($_.Exception.Message)，正在恢复原目录……"
        if (Test-Path -LiteralPath $DataDir -PathType Container) {
            Remove-Item -LiteralPath $DataDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        if ($movedOld -and (Test-Path -LiteralPath $oldDir -PathType Container)) {
            Move-Item -LiteralPath $oldDir -Destination $DataDir -Force
            Write-Ok "已恢复回滚前的数据目录"
        }
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
        Fail "回滚失败，当前数据目录已恢复原样。请检查磁盘空间 / 文件占用后重试。"
    }
    Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    Write-Ok "数据目录已恢复：$DataDir"
    Write-Note "回滚前的数据保留在：$oldDir（确认无误后可手动删除）"
}

# ── 6. 结果 ───────────────────────────────────────────────────
Write-Step "5/5 完成"
$manifestPath = $Backup + ".manifest.json"
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-Note "备份创建时间：$($manifest.created_at)；备份时版本：$($manifest.automind_version)"
        if ($manifest.automind_version) {
            Write-WarnMsg "如果当前安装的程序版本与备份时版本（$($manifest.automind_version)）不一致，请按 docs\OFFLINE_OPS.md 的“升级 / 回滚”一节同步安装对应版本。"
        }
    }
    catch {
        Write-Note "无法解析 manifest.json（已忽略）"
    }
}
if ($script:Dry) {
    Write-WarnMsg "DRY-RUN：未做任何实际修改。去掉 -WhatIf / -DryRun 后重跑即可。"
}
else {
    Write-Host ""
    Write-Host "回滚完成。" -ForegroundColor Green
    Write-Host "  启动服务: powershell -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $script:ScriptDir 'verify.ps1')`""
}
exit 0
