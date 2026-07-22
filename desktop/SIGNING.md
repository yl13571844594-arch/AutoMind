# AutoMind 代码签名操作手册（Certum SimplySign 云证书）

> 本文记录 2026-07 首次全签名发布的完整实操经过与踩坑，供下次直接照做。
> 证书主体：西安心途启境科技有限公司 · 签发：Certum Code Signing 2021 CA。

## 一、这是什么类型的证书？（先搞清楚，否则方向全错）

自 2023-06 CA/B 论坛新规，**所有代码签名证书私钥必须存于硬件**（云 HSM 或
密码卡），**不再有可直接使用的 PFX 文件**。Certum 证书两种形态：

- **SimplySign 云签名**（本项目所用）：私钥在 Certum 云端 HSM；桌面装
  **SimplySign Desktop**，用手机 App 的 OTP 登录建立会话后，证书出现在
  Windows 证书库（`Cert:\CurrentUser\My`），signtool 用**指纹**引用它即可，
  私钥运算由 Certum CSP 透明委托到云端。
- 物理密码卡（proCertum CardManager）：插卡后同理，证书进证书库。

**判断当前形态**（PowerShell）：
```powershell
Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | fl Subject,Issuer,Thumbprint,NotAfter,HasPrivateKey
Get-Process | Where-Object Name -match 'SimplySign|CardManager'
```
本机结果：证书指纹 `0A6C8F03EB71C7E940955388D257CB7EF329C24B`，
`SimplySignDesktop.exe` 运行中 → **SimplySign 云证书，用指纹签名**。

## 二、前置条件（缺一不可）

1. **SimplySign Desktop 已登录**（手机 App 扫码/OTP）。这是私钥能被访问的
   前提；证书库里 `HasPrivateKey=True` 只是密钥容器指针，**不代表已登录**。
   验证方式：直接试签（见下），出现 `Done Adding Additional Store` 即会话有效。
2. **signtool.exe**。本机无 Windows SDK，改从 NuGet 包
   `Microsoft.Windows.SDK.BuildTools` 抽取（CI 标准做法）：
   ```powershell
   $ver = "10.0.26100.1742"
   curl.exe -sL --ssl-no-revoke -o sdk.zip `
     "https://api.nuget.org/v3-flatcontainer/microsoft.windows.sdk.buildtools/$ver/microsoft.windows.sdk.buildtools.$ver.nupkg"
   Expand-Archive sdk.zip -DestinationPath pkg
   # signtool 在 pkg\bin\<ver>\x64\signtool.exe（542KB，签 PE 仅需它 + 系统 DLL）
   ```
   已固定安装在 `%LOCALAPPDATA%\AutoMindBuildTools\bin\signtool.exe`。

## 三、踩坑与解法（本机网络专属）

**坑：时间戳服务器全部"不可达 / 无效响应"，但签名本身成功。**

- 根因：signtool 的 RFC3161 时间戳走 **WinHTTP**；本机 `netsh winhttp show proxy`
  为「直连」，而外网需经本地代理 `127.0.0.1:22307`（Clash/Oray 类）。于是
  签名（本地 CSP+云 HSM）成功，时间戳（外网 POST）失败。
- 解法：**签名期间临时把 WinHTTP 指向本地代理，签完还原**。已封装进
  `sign.ps1` 的 `AUTOMIND_TS_PROXY` 环境变量（try/finally 保证还原）。
- 时间戳服务器实测：`timestamp.sectigo.com` 经代理稳定；`time.certum.pl`
  偶发无效响应。故 sign.ps1 里 **sectigo 首选、certum 回退**。

**PIN/OTP**：全程无需在命令行输入。SimplySign 会话有效期内 signtool 静默
签名；若会话过期会弹 SimplySign Desktop 界面要求重新登录 —— 由人工完成，
脚本永不经手密码。

## 四、一键全签名发布（配方已固化）

```powershell
$env:AUTOMIND_SIGNTOOL        = "$env:LOCALAPPDATA\AutoMindBuildTools\bin\signtool.exe"
$env:AUTOMIND_CERT_THUMBPRINT = "0A6C8F03EB71C7E940955388D257CB7EF329C24B"
$env:AUTOMIND_TS_PROXY        = "127.0.0.1:22307"   # 本机专属；换网络时按需调整
cd desktop
.\build_release.ps1 -SkipWeb    # 前端未改用 -SkipWeb；改了则去掉
```

流水线：版本一致性检查 → PyInstaller 冻结 → **签 AutoMind.exe** →
Inno 打包（`/DSIGN` 令其**连签 Setup.exe 与卸载器**）→ 全部验签。
产物：`desktop\Output\AutoMind-Setup-<ver>.exe`（全签名）。

单独签某文件 / 仅验签：
```powershell
.\sign.ps1 -Path dist\AutoMind\AutoMind.exe          # 签
.\sign.ps1 -Path Output\AutoMind-Setup-1.2.0.exe -VerifyOnly   # 验
```

## 五、验签成功的样子

```
Issued to: 西安心途启境科技有限公司   （链：Certum Trusted Network CA 2 → Certum Code Signing 2021 CA）
The signature is timestamped: <时间>
Timestamp Verified by: Sectigo Public Time Stamping ...
Successfully verified: ...
```

## 六、下次续期 / 换机注意

- 证书 2027-07-21 到期。**已签且带 RFC3161 时间戳的旧包，到期后仍有效**
  （时间戳锁定了"签名时证书有效"这一事实）。
- 换机器：重装 SimplySign Desktop 并登录即可，指纹不变（同一证书）。
- SmartScreen 信誉：OV 证书需累积下载量才逐步消除首次运行蓝色提示；
  这是 OV 的固有特性（EV 证书可立即消除，但更贵）。
