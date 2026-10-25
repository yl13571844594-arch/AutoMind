# AutoMind Pro 一键升级包（.ampkg）发行脚本（v1.11.14）
#
# 等价于 scripts/build_pro_upgrade.py：
#   构建 wheel → 版本一致性校验 → doctor 自检（公钥/私钥/wheel/bundle）→
#   签发许可证并打成 .ampkg → 打印 sha256 与客户升级步骤。
#
# 前置（发行方一次性）：python pro/tools/issue_license.py keygen --write-public-key
# 私钥不得提交：只放在 pro/.license-private/（已 gitignore）或环境变量里。
#
# 示例：
#   .\scripts\build_pro_upgrade.ps1 -Tier PRO -Expiry 20271231 -Customer acme
#   .\scripts\build_pro_upgrade.ps1 -Tier ENT -Expiry 00000000 -Customer bigcorp -Seats 50
#requires -Version 5.1
param(
    [ValidateSet('PRO', 'ENT')][string]$Tier = 'PRO',
    [string]$Expiry = '00000000',
    [string]$Customer = 'acme',
    [int]$Seats = 0,
    [string]$Out = '',
    [switch]$NoBuild
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root

$pyArgs = @(
    'scripts/build_pro_upgrade.py',
    '--tier', $Tier,
    '--expiry', $Expiry,
    '--customer', $Customer,
    '--seats', [string]$Seats
)
if ($Out) { $pyArgs += @('--out', $Out) }
if ($NoBuild) { $pyArgs += '--no-build' }

python @pyArgs
exit $LASTEXITCODE
