<#
.SYNOPSIS
    AutoMind 数据目录备份脚本（Windows PowerShell 5.1+）。

.DESCRIPTION
    把数据目录（配置 / 数据库 / 知识库 / 日志）、主配置文件与许可证一起打成
    带时间戳的 zip，并生成 SHA-256 校验清单（.sha256）与 JSON 清单（.manifest.json）。
    支持保留策略：按天数删除过旧备份，但至少保留最近 N 份。

    备份前请先停止 AutoMind 服务：SQLite 文件在运行中被占用时压缩会失败；
    脚本会给出明确提示，而不是留下一个半截的坏包。

.PARAMETER DataDir
    数据目录。默认沿用 AUTOMIND_DATA_DIR，其次 %APPDATA%\AutoMind，最后 .\.automind。

.PARAMETER OutDir
    备份输出目录。默认 <DataDir>\backups。

.PARAMETER ConfigFile
    主配置文件。默认自动探测 <DataDir>\config.json、<DataDir>\..\config.json 与当前目录的 .automind_config.json。

.PARAMETER LicenseFile
    许可证文件。默认自动探测 .automind_license（当前目录 / 数据目录）。

.PARAMETER RetainDays
    删除超过该天数的备份（默认 30 天；0 = 不按天数删除）。

.PARAMETER KeepLast
    无论多旧，至少保留最近 N 份备份（默认 5）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\backup.ps1 -DataDir D:\AutoMind

.EXAMPLE
    powershell -NoProfile -File .\backup.ps1 -DataDir D:\AutoMind -OutDir E:\backups -RetainDays 90 -WhatIf
#>
[CmdletBinding()]
param(
    [string]$DataDir = "",
    [string]$OutDir = "",
    [string]$ConfigFile = "",
    [string]$LicenseFile = "",
    [int]$RetainDays = 30,
    [int]$KeepLast = 5,
    [switch]$WhatIf,
    [Alias("dry-run")]
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$script:Dry = [bool]($WhatIf -or $DryRun)

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
function Invoke-DryAware([string]$Description, [scriptblock]$Action) {
    if ($script:Dry) {
        Write-Host "    [DRY-RUN] $Description" -ForegroundColor DarkGray
        return
    }
    Write-Host "    -> $Description"
    & $Action
}

Write-Step "AutoMind 数据备份"
Add-Type -AssemblyName System.IO.Compression.FileSystem

# ── 1. 解析数据目录 ─────────────────────────────────────────────
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
if (-not (Test-Path -LiteralPath $DataDir -PathType Container)) {
    Fail "数据目录不存在：$DataDir。请用 -DataDir 指定 AutoMind 实际使用的数据目录（见 install.ps1）。"
}
if (-not $OutDir) {
    $OutDir = Join-Path $DataDir "backups"
}
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
Write-Note "数据目录：$DataDir"
Write-Note "输出目录：$OutDir"

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$zipName = "automind-backup-" + $stamp + ".zip"
$zipPath = Join-Path $OutDir $zipName
$staging = Join-Path ([System.IO.Path]::GetTempPath()) ("automind-backup-staging-" + $stamp)

# ── 2. 收集要备份的内容 ─────────────────────────────────────────
Write-Step "1/4 收集配置 / 许可证 / 数据目录"
$configCandidates = @()
if ($ConfigFile) { $configCandidates += $ConfigFile }
$configCandidates += (Join-Path $DataDir "config.json")
$configCandidates += (Join-Path (Split-Path -Parent $DataDir) "config.json")
$configCandidates += (Join-Path (Get-Location).Path ".automind_config.json")
$licenseCandidates = @()
if ($LicenseFile) { $licenseCandidates += $LicenseFile }
$licenseCandidates += (Join-Path (Get-Location).Path ".automind_license")
$licenseCandidates += (Join-Path $DataDir ".automind_license")

$foundConfig = @($configCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) })
$foundLicense = @($licenseCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) })
Write-Note ("配置文件：" + $(if ($foundConfig.Count -gt 0) { $foundConfig[0] } else { "未找到（跳过）" }))
Write-Note ("许可证  ：" + $(if ($foundLicense.Count -gt 0) { $foundLicense[0] } else { "未找到（跳过）" }))
$dataChildren = @(Get-ChildItem -LiteralPath $DataDir -Force | Where-Object { $_.FullName -ne $OutDir })
Write-Note "数据目录条目：$($dataChildren.Count) 个"
if ($dataChildren.Count -eq 0 -and $foundConfig.Count -eq 0) {
    Fail "数据目录为空且没有找到主配置文件：$DataDir。请确认 -DataDir 是否正确。"
}

# ── 3. 打包 + SHA-256 清单 ──────────────────────────────────────
Write-Step "2/4 生成 zip 与 SHA-256 清单"
Invoke-DryAware "创建临时目录 $staging" {
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $staging "data") -Force | Out-Null
}
if (-not $script:Dry) {
    foreach ($child in $dataChildren) {
        Copy-Item -LiteralPath $child.FullName -Destination (Join-Path $staging "data") -Recurse -Force
    }
    if ($foundConfig.Count -gt 0) {
        $configDest = Join-Path $staging "config"
        New-Item -ItemType Directory -Path $configDest -Force | Out-Null
        Copy-Item -LiteralPath $foundConfig[0] -Destination $configDest -Force
    }
    if ($foundLicense.Count -gt 0) {
        $licenseDest = Join-Path $staging "license"
        New-Item -ItemType Directory -Path $licenseDest -Force | Out-Null
        Copy-Item -LiteralPath $foundLicense[0] -Destination $licenseDest -Force
    }
    Invoke-DryAware "创建输出目录 $OutDir" {
        New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    }
    try {
        [System.IO.Compression.ZipFile]::CreateFromDirectory(
            $staging, $zipPath,
            [System.IO.Compression.CompressionLevel]::Optimal, $false)
    }
    catch {
        Fail ("压缩失败：$($_.Exception.Message)。最常见原因是 AutoMind 服务还在运行、SQLite 文件被占用 —— " +
              "请先停止服务（可用 rollback.ps1 -Port 8765 同款方式结束监听进程，或手动结束 python 进程）后重跑本脚本。")
    }
    Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
}
else {
    Write-Host "    [DRY-RUN] 将压缩 $staging -> $zipPath" -ForegroundColor DarkGray
}

if (-not $script:Dry) {
    $hash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $zipSize = (Get-Item -LiteralPath $zipPath).Length
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $shaFile = $zipPath + ".sha256"
    [System.IO.File]::WriteAllText($shaFile, ($hash + "  " + $zipName + "`n"), $utf8NoBom)
    $installedVersion = ""
    $configCandidates = @(
        (Join-Path $DataDir "config\offline-config.json"),
        (Join-Path (Split-Path -Parent $DataDir) "config\offline-config.json")
    )
    $configJsonPath = $configCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
    if ($configJsonPath) {
        try {
            $cfg = Get-Content -LiteralPath $configJsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($cfg.version) { $installedVersion = [string]$cfg.version }
        }
        catch { $installedVersion = "" }
    }
    $manifest = [ordered]@{
        created_at        = (Get-Date).ToString("o")
        file              = $zipName
        size_bytes        = $zipSize
        sha256            = $hash
        data_dir          = $DataDir
        automind_version  = $installedVersion
        retained_days     = $RetainDays
        keep_last         = $KeepLast
    } | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText(($zipPath + ".manifest.json"), $manifest, $utf8NoBom)
    Write-Ok "zip：$zipPath（$([math]::Round($zipSize / 1MB, 2)) MB）"
    Write-Ok "SHA256：$hash"
}

# ── 4. 保留策略 ─────────────────────────────────────────────────
Write-Step "3/4 应用保留策略（保留最近 $KeepLast 份，删除超过 $RetainDays 天的旧备份）"
if (Test-Path -LiteralPath $OutDir) {
    $backups = @(Get-ChildItem -LiteralPath $OutDir -Filter "automind-backup-*.zip" |
            Sort-Object LastWriteTime -Descending)
    $index = 0
    $toDelete = @()
    foreach ($item in $backups) {
        $index++
        if ($index -le $KeepLast) { continue }
        if ($RetainDays -gt 0) {
            $ageDays = ((Get-Date) - $item.LastWriteTime).TotalDays
            if ($ageDays -gt $RetainDays) { $toDelete += $item }
        }
    }
    if ($toDelete.Count -eq 0) {
        Write-Note "没有需要清理的旧备份（共 $($backups.Count) 份）"
    }
    foreach ($item in $toDelete) {
        Invoke-DryAware "删除旧备份 $($item.Name)" {
            Remove-Item -LiteralPath $item.FullName -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath ($item.FullName + ".sha256") -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath ($item.FullName + ".manifest.json") -Force -ErrorAction SilentlyContinue
        }
    }
}
else {
    Write-Note "输出目录尚不存在，跳过保留策略"
}

Write-Step "4/4 完成"
if ($script:Dry) {
    Write-WarnMsg "DRY-RUN：未真正创建备份文件。去掉 -WhatIf / -DryRun 后重跑即可。"
}
else {
    Write-Host ""
    Write-Host "备份完成。" -ForegroundColor Green
    Write-Host "  文件  : $zipPath"
    Write-Host "  校验  : $zipPath.sha256（sha256sum 兼容格式）"
    Write-Host "  清单  : $zipPath.manifest.json"
    Write-Host "  回滚  : powershell -NoProfile -File `"$(Join-Path $PSScriptRoot 'rollback.ps1')`" -Backup `"$zipPath`" -Yes"
}
exit 0
