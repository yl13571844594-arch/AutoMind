# 内网 / 离线部署运维手册（v1.11.14）

> 面向：客户 IT / 交付实施 / 运维值班。
> 目标：**在无外网（或只有受控出网）的 Windows 机器上，把 AutoMind 装好、出网管住、
> 数据可备份可回滚、状态可验收**。每条命令都可直接复制执行。
>
> 设计背景与链路分析见 [OFFLINE_DEPLOYMENT.md](OFFLINE_DEPLOYMENT.md)；
> 商业版激活（许可证 v2 / `.ampkg`）见 [COMMERCIAL_ACTIVATION.md](COMMERCIAL_ACTIVATION.md)。

---

## 0. 一页速查

| 事项 | 命令 / 位置 |
|---|---|
| 离线安装 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\offline\install.ps1 -Package <wheel目录> -DataDir <数据目录> -Mode offline` |
| 备份 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\offline\backup.ps1 -DataDir <数据目录>` |
| 回滚 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\offline\rollback.ps1 -Backup <备份.zip> -Yes` |
| 验收 | `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\offline\verify.ps1 -ExpectMode offline` |
| 策略判定（冒烟） | `python scripts\offline\smoke_offline.py --json` |
| 出网审计 | `<数据目录>\logs\egress.log`（JSONL） |
| 诊断工具 | Agent 里调用 `net_check`；运维可看 `GET /api/health` 的 `network_policy` / `network` |
| 关键环境变量 | `AUTOMIND_OFFLINE`、`AUTOMIND_NETWORK_POLICY`、`AUTOMIND_EGRESS_ALLOW(_FILE)`、`AUTOMIND_EGRESS_DENY`、`AUTOMIND_PROBE_HOSTS`、`AUTOMIND_GATEWAY_PROBE`、`AUTOMIND_LOCAL_PROBES` |

> 脚本全部兼容 **Windows PowerShell 5.1**（无需 PowerShell 7），支持 `-WhatIf` / `--dry-run`。
> 建议所有脚本参数包含路径时加引号；脚本文件本身是 UTF-8 **带 BOM**，请勿用会去掉 BOM 的编辑器另存。

---

## 1. 部署前检查

### 1.1 机器与环境

- [ ] Windows 10/11（PowerShell 5.1 自带）；如用 Server 版同样可用。
- [ ] Python **3.11+**（`python --version`）；推荐 3.12。
- [ ] 磁盘：数据目录 ≥ 1 GB 可用（含日志 / 备份），安装盘 ≥ 500 MB。
- [ ] 端口：默认 `127.0.0.1:8765`（**只绑回环**，不要 `0.0.0.0`）。
- [ ] 时间同步（审计日志、许可证到期日都依赖本地时间）。
- [ ] 杀毒 / 终端管控软件已把安装目录、数据目录加入白名单（否则 SQLite 文件会被锁）。

### 1.2 离线包准备（在能上网的机器上做，整目录拷贝进内网）

```powershell
# 在联网机器上执行（Python 版本与目标机一致，例如 3.12）
python -m pip download "automind-agent[web]==1.11.14" -d .\offline-wheels
# 若目标机需要 Pro 版：把发行方给的 AutoMind-PRO-*.ampkg 一起拷入
```

拷贝清单（内网侧）：

```text
offline-wheels\            ← pip download 的全部 .whl
scripts\offline\           ← 本手册配套脚本（install/backup/rollback/verify/smoke_offline.py）
egress-allow.txt           ← 出网白名单（按第 3 节准备，可留空先 fail-closed）
AutoMind-PRO-*.ampkg       ← 商业版升级包（可选）
```

### 1.3 需要向客户网络组确认的放通项（allowlist 模式才需要）

| 用途 | 域名 | 不放通会怎样 |
|---|---|---|
| 模型 API | `api.deepseek.com` / 内网 relay 域名 | 任务无法推理（本地 Ollama 不需要） |
| 飞书 | `open.feishu.cn`（长连接） | 机器人收不到 / 回不了消息 |
| 钉钉 | `api.dingtalk.com`（Stream） | 同上 |
| 企业微信 | `qyapi.weixin.qq.com` | 同上 |
| 依赖下载 | `pypi.tuna.tsinghua.edu.cn` / `registry.npmmirror.com` | 只在**安装期**需要；离线 wheel 方案不需要 |
| 自建服务 | 内网 SearXNG / 内网 relay | 内网目标在策略层默认放行（不受白名单限制），但需要网络可达 |

> 白名单**最小集**原则：宁少勿多。先用空白名单上线（全部拒绝），
> 再按 `egress.log` 里的拒绝记录逐条放通，最后跑 `verify.ps1` 留档。

---

## 2. 安装（离线）

### 2.1 推荐：用 install.ps1（幂等，失败回滚）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline\install.ps1 `
    -Package .\offline-wheels `
    -DataDir D:\AutoMind `
    -Mode offline `
    -RegisterAutostart

# 先看计划再执行：
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline\install.ps1 `
    -Package .\offline-wheels -DataDir D:\AutoMind -Mode offline -WhatIf
```

`install.ps1` 会依次做（任一步失败即停止并回滚安装目录）：

1. 校验 Python ≥ 3.11，读取已安装版本；
2. 备份当前 `automind` 包目录（升级场景），确保失败可回滚；
3. `pip install --no-index --find-links <目录>`（**不发任何外网请求**）；
4. 安装后校验 `automind.__version__ == 1.11.14`，不一致自动恢复；
5. 写 `<数据目录>\config\offline-config.json` 与白名单文件（allowlist 模式）；
6. 写**用户级**环境变量：`AUTOMIND_NETWORK_POLICY` / `AUTOMIND_OFFLINE` /
   `AUTOMIND_DATA_DIR` / `AUTOMIND_EGRESS_ALLOW_FILE` / `AUTOMIND_EGRESS_AUDIT`
   （`-NoPersistEnv` 可只对当前进程生效）；
7. 可选注册开机自启（HKCU Run，当前用户级，不需要管理员）；
8. 跑 `smoke_offline.py --json` 做安装后冒烟自检。

### 2.2 常用参数

| 参数 | 说明 |
|---|---|
| `-Package` | wheel 目录（推荐）或单个 `.whl`；不传则只写配置（适合已装好的机器） |
| `-DataDir` | 数据目录；默认 `%APPDATA%\AutoMind` |
| `-Mode offline\|allowlist\|open` | 出网策略，默认 offline |
| `-EgressAllow <文件>` | 白名单文件，复制到 `<数据目录>\config\egress-allow.txt` |
| `-ProbeHosts host:port,...` | 写 `AUTOMIND_PROBE_HOSTS`（内网诊断目标） |
| `-RegisterAutostart` / `-RemoveAutostart` | 注册 / 移除开机自启 |
| `-Force` | 版本一致也强制重装 |
| `-WhatIf` / `-DryRun` | 只打印计划，不做任何修改 |

### 2.3 手动安装（不用脚本时）

```powershell
python -m pip install --no-index --find-links .\offline-wheels "automind-agent[web]==1.11.14"
$env:AUTOMIND_OFFLINE = "1"
$env:AUTOMIND_DATA_DIR = "D:\AutoMind"
python -m automind.server --host 127.0.0.1 --port 8765
```

> 手动方式不会写用户级环境变量，重启终端后需重新设置；长期部署建议用 `install.ps1`。

### 2.4 安装后 3 分钟自检

```powershell
python -c "import automind; print(automind.__version__)"     # 应输出 1.11.14
python scripts\offline\smoke_offline.py --json               # ok 应为 true
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline\verify.ps1 -ExpectMode offline -SkipServiceCheck
```

---

## 3. 出网策略与白名单维护

### 3.1 三种模式怎么选

| 模式 | 开法 | 适用 | 行为 |
|---|---|---|---|
| `offline` | `AUTOMIND_OFFLINE=1` 或 `AUTOMIND_NETWORK_POLICY=offline` | 完全无外网；本地模型 / 内网服务 | 公网出网**当场拒绝**；本地模型、内网 SearXNG、内网 relay 正常 |
| `allowlist` | `AUTOMIND_NETWORK_POLICY=allowlist` + `AUTOMIND_EGRESS_ALLOW` | 有受控出网（IM / 云端模型） | 仅白名单目标可出网，其余拒绝 |
| `open` | 默认 / `AUTOMIND_NETWORK_POLICY=open` | 单机自用、没有合规要求 | 与历史版本一致，不做限制（但仍防 SSRF） |

优先级：`AUTOMIND_NETWORK_POLICY`（显式） > `AUTOMIND_OFFLINE` > 默认 `open`。
显式 policy 写了非法值 → **fail-closed 按 offline**（安全开关写坏不会静默放开）。

> 注意：策略在**进程启动时**读取环境变量与白名单文件。改完白名单后需要
> **重启 AutoMind 服务**，或让 Agent 新会话（新进程）生效。

### 3.2 白名单格式

`AUTOMIND_EGRESS_ALLOW` 是逗号 / 分号分隔的字符串；`AUTOMIND_EGRESS_ALLOW_FILE`
是文件，每行一条，`#` 之后是注释。两者**叠加**，不分大小写。支持的写法：

```text
# 主机：任意端口
api.deepseek.com

# 主机 + 端口（只放通该端口）
open.feishu.cn:443

# 通配：example.com 及其所有子域
*.example.com

# IP / 网段
192.168.10.20
10.10.0.0/16

# 也接受带协议的写法（只取主机 + 端口）
https://api.deepseek.com/v1
```

拒绝列表 `AUTOMIND_EGRESS_DENY` 格式相同、**优先级高于白名单**（open 模式下也生效），
用于"全局禁用遥测类域名"这类场景。

### 3.3 内网目标为什么不在白名单里也放行？

`offline` / `allowlist` 管的是**出公网**，不是"不许访问内网"：本地模型
（`127.0.0.1:11434`）、内网 SearXNG、内网 relay、内网 Web 系统都是合法的。
策略层对回环 / 私网 IP / `localhost` / `.local` `.corp` `.internal` 等内网域名 /
单标签主机名默认放行。

要更严格（**内网也必须显式白名单**）时设 `AUTOMIND_EGRESS_STRICT=1`；
此时内网目标也按 allowlist 判定，offline 下内网出网同样被拒。

### 3.4 出网审计

每次放行 / 拒绝都会写一行 JSONL 到 `<数据目录>\logs\egress.log`：

```json
{"ts":"2026-10-21T12:00:00.123+00:00","event":"deny","tool":"http_request",
 "target":"https://api.deepseek.com:443/v1","host":"api.deepseek.com","port":443,
 "mode":"offline","source":"AUTOMIND_OFFLINE","reason":"offline_policy"}
```

- **脱敏**：只记 `scheme://host:port/path` 前 200 字符；`?api_key=…` 之类的 query 与
  URL 里的 `user:pass@` 一律不落盘。
- 关闭审计：`AUTOMIND_EGRESS_AUDIT=0`（不推荐，验收会检查审计开启）。
- 排障常用：`Get-Content <数据目录>\logs\egress.log -Tail 50`；
  `verify.ps1` 会断言"离线 / 白名单模式下没有可疑公网放行记录"。

### 3.5 白名单维护流程（建议写进变更单）

1. 从 `egress.log` 收集 `event=deny` 的 `host`（或 `net_check` 的失败目标）；
2. 评估"这个域是否必须"：能走内网 relay / 本地模型的就不要放公网；
3. 编辑 `<数据目录>\config\egress-allow.txt`（每行一条，注释写清业务原因与变更单号）；
4. 重启 AutoMind 服务；
5. 跑 `verify.ps1` 留档；验证目标功能；
6. 定期复核：删除 30 天未出现的白名单条目。

---

## 4. 探测三态与诊断

### 4.1 net_check 输出怎么读

在对话里让 Agent 调用 `net_check`（或直接看返回 JSON），核心字段：

| 字段 | 含义 |
|---|---|
| `state` | `online` / `expected_offline` / `degraded` |
| `policy` | 当前出网策略（mode / allow / deny / 审计开关） |
| `sections` | 三段式结论：`local`（本地服务）/ `gateway`（网关、relay）/ `public`（公网） |
| `local` / `gateway` / `public` | 每段的配置、可达性、DNS / 连接明细 |
| `advice` | 可执行建议（离线时明确写"预期，不要重试"） |
| `next` | 给 Agent 的下一步指令（避免它反复空等 / 重试） |

**三态语义**（客户评审时重点解释这一条）：

- `online`：公网可达，正常；
- `expected_offline`：**离线策略下公网不可达是预期**，不是故障，不需要重试 / 等待；
- `degraded`：配置了网关或本地服务但不可达 —— **这是真故障**，按 advice 排查。

优先级：故障 > 预期离线 > 在线。离线部署里"网关不通"仍然是故障，不会被说成预期。

### 4.2 探测目标配置（内网部署务必配）

```powershell
# 公网探测目标（不配则用默认国内外站点；内网里默认目标本来就不可达）
$env:AUTOMIND_PROBE_HOSTS = "mirror.corp.local:443,relay.corp.local:443"
# 网关 / relay 探测（可选，配了才能把"网关故障"和"公网不可达"分开）
$env:AUTOMIND_GATEWAY_PROBE = "relay.corp.local:443"
# 本地服务探测（可选，如 Web UI / 本地模型）
$env:AUTOMIND_LOCAL_PROBES = "127.0.0.1:8765,127.0.0.1:11434"
```

写进用户环境变量（install.ps1 的 `-ProbeHosts` 会写第一个）：

```powershell
[Environment]::SetEnvironmentVariable("AUTOMIND_PROBE_HOSTS", "relay.corp.local:443", "User")
```

### 4.3 /api/health

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/health | ConvertTo-Json -Depth 5
```

新增三块：

- `network_policy`：`mode` / `source` / `allow` / `deny` / `audit` / `audit_log`；
- `network`：三态快照（`state` / `reason` / `local` / `gateway` / `public` / `advice`），
  走缓存 + 后台刷新，**不会拖慢探针**；
- `egress_audit`：本次进程内放行 / 拒绝计数 + 审计日志路径与开关状态。

### 4.4 常见场景对照

| 现象 | net_check 结论 | 处理 |
|---|---|---|
| 离线部署、公网全红 | `expected_offline`，public 段"预期离线" | 正常，不需要处理；不要再跑 `wait_for_network` |
| 配了 relay 但模型调不通 | `degraded`，gateway 段不可达 | 查 relay 进程 / 端口 / `AUTOMIND_GATEWAY_PROBE` / 代理 |
| 本地 Web UI 打不开 | `degraded`，local 段不可达 | 查 AutoMind 服务是否启动、端口是否被占 |
| 只有某些境外站不通 | `online`（公网可达） | 是目标域问题，不是本机断网；按 allowlist / 镜像处理 |
| 所有目标 DNS 失败 | `degraded`，明细里 `dns: false` | 查 DNS / hosts / 能否解析内网域名 |

---

## 5. 备份策略

### 5.1 备份什么

`backup.ps1` 备份：数据目录全部内容（配置 / 数据库 / 知识库 / 工作区 / 日志 /
扩展包 `extensions\`）、主配置文件（`.automind_config.json` 或 `<数据目录>\config.json`）、
许可证文件（`.automind_license`，若存在）。产物：

```text
automind-backup-20261021-120000.zip           ← 数据本体
automind-backup-20261021-120000.zip.sha256    ← 校验清单（sha256sum 兼容格式）
automind-backup-20261021-120000.zip.manifest.json  ← 版本 / 大小 / sha256 / 保留策略
```

### 5.2 怎么备

```powershell
# 默认输出到 <数据目录>\backups，保留最近 5 份、删除 30 天前的
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline\backup.ps1 -DataDir D:\AutoMind

# 备到异地盘 / 网络盘，保留 90 天、最少留 10 份
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline\backup.ps1 `
    -DataDir D:\AutoMind -OutDir E:\backups -RetainDays 90 -KeepLast 10
```

> **运行中备份**：SQLite 文件可能被占用导致压缩失败。脚本会明确报错并提示先停服务；
> 最佳实践是停服务后备份（`rollback.ps1` 的停服务逻辑同样可用），或由运维在维护窗口做。

### 5.3 备份规范（建议写进 SOP）

- **频率**：每天一次自动备份 + 每次升级/改配置前手动备份；
- **保留**：本地最近 7 天 + 异地最近 90 天（脚本保留策略只管本地目录，异地拷贝用
  任务计划或备份软件做）；
- **校验**：每月抽一个备份做恢复演练（在测试数据目录上跑 `rollback.ps1`）；
- **加密**：备份含 API Key 与许可证，离开本机前应放加密盘 / 加密压缩；
- **离线**：备份文件不要只放同一台机器（磁盘坏了就一起没了）。

### 5.4 恢复演练

```powershell
# 在隔离环境里：用备份恢复到临时数据目录
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline\rollback.ps1 `
    -Backup E:\backups\automind-backup-20261021-120000.zip `
    -DataDir D:\AutoMind-test -SkipStop -Yes
```

---

## 6. 升级

### 6.1 离线升级（社区版 / 新 wheel）

1. 停服务；
2. `backup.ps1` 备份数据（必须）；
3. 把新版 wheel 目录拷进内网；
4. `install.ps1 -Package <新版目录> -Force`（自动备份旧包、失败回滚、装后冒烟）；
5. `verify.ps1 -ExpectedVersion <新版本>` 验收；
6. `rollback.ps1 -Backup <升级前备份>` 可整体回退（数据 + 若同时保留旧 wheel 可重装旧版）。

> 版本一致性：升级包与主程序必须**同版本**（`.ampkg` 清单会校验 version_match）。

### 6.2 商业版激活 / 续期（许可证 v2）

- 许可证是**离线 Ed25519 签名**（一行 `AMP2-…`）：客户机只内置验签公钥，
  私钥在发行方手里；断网也能验签，客户无法伪造 / 改档位 / 改到期日。
- 旧 v1 格式 `AMP-…`（对称 HMAC）**默认拒绝**（那套密钥历史随包分发过，等同公开）。
- 交付方式：发行方执行 `python pro/tools/issue_license.py bundle` 生成
  `AutoMind-PRO-<客户>-<到期日>.ampkg`（wheel + 许可证 + 清单打成一个文件）。
- 客户侧：应用内「设置 → 升级专业版 / 企业版」→ 把 `.ampkg` 拖进「⚡ 一键升级」面板
  → 看到"已升级"即完成（提示重启则重启一次）。
- 续期 / 升档同样是"再发一个新的 `.ampkg`"，拖进面板即可。

详见 [COMMERCIAL_ACTIVATION.md](COMMERCIAL_ACTIVATION.md)（发行方密钥纪律、
`doctor` 自检、sha256 台账、排障表）。

### 6.3 升级失败怎么办

| 现象 | 处理 |
|---|---|
| `pip` 报缺依赖（`No matching distribution`） | 离线包目录缺依赖 wheel；回联网机重新 `pip download "automind-agent[web]==x.y.z"` |
| 安装后版本不一致 | `install.ps1` 会自动回滚；核对 wheel 文件名与 `-ExpectedVersion` |
| 服务起不来 | 先 `rollback.ps1` 回数据，再重装旧版 wheel；用 `verify.ps1` 定位 |
| `.ampkg` 提示版本不一致 | 扩展包（wheel）版本必须等于主程序版本；让发行方重新 bundle |

---

## 7. 回滚

### 7.1 用 rollback.ps1（推荐）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline\rollback.ps1 `
    -Backup E:\backups\automind-backup-20261021-120000.zip `
    -DataDir D:\AutoMind -Yes
```

执行顺序（**怎么都不会把当前状态弄丢**）：

1. 校验备份包 SHA-256（有 `.sha256` 清单时强制比对，损坏直接拒绝）；
2. 按端口（`-Port`，默认 8765）找到监听进程并停止；
3. **先备份当前状态**（自动调用 `backup.ps1` 生成 pre-rollback 备份）；
4. 解压到临时目录 → 旧数据目录改名保留 → 新目录就位（原子替换）；
5. 任一步失败 → 把旧目录改回来并报失败点。

常用参数：`-SkipStop`（已手动停服务）、`-Yes`（无人值守）、`-WhatIf` / `-DryRun`（演练）。

> 回滚完成后旧数据保留在 `<数据目录>.before-rollback-<时间戳>`，确认无误后可删除。
> 回滚的是**数据**；如果同时升级了程序版本，需要再装回对应版本的 wheel。

### 7.2 手动回滚（脚本不可用时）

```powershell
# 0) 先停掉监听 8765 的进程
Get-NetTCPConnection -LocalPort 8765 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
# 1) 当前数据改名保留（不要直接删！）
Rename-Item D:\AutoMind D:\AutoMind.before-rollback-20261021
# 2) 解压备份
Expand-Archive E:\backups\automind-backup-20261021-120000.zip -DestinationPath $env:TEMP\rollback
# 3) 备份包内 data\ 是数据目录内容
Move-Item $env:TEMP\rollback\data D:\AutoMind
# 4) 启动并验收
python -m automind.server --host 127.0.0.1 --port 8765
```

### 7.3 回滚后验收

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline\verify.ps1 -ExpectMode offline
```

---

## 8. 故障排查

| 症状 | 可能原因 | 处理 |
|---|---|---|
| 所有公网工具都返回"出网被策略拒绝" | 处于 offline / allowlist 且目标未放通（**预期行为**） | 确认模式；把目标加入 `AUTOMIND_EGRESS_ALLOW` 后重启服务 |
| 改了白名单不生效 | 策略在进程启动时读取；文件路径不对；格式写错 | 重启服务；`policy_status()` / `/api/health.network_policy.allow` 核对读到的条目 |
| `smoke_offline.py` 报 `policy.mode` 与期望不符 | 环境变量优先级：显式 `AUTOMIND_NETWORK_POLICY` > `AUTOMIND_OFFLINE` | 检查两处变量；用 `[Environment]::GetEnvironmentVariable(name,"User")` 看持久化值 |
| net_check 全部目标不可达 | 内网本来没有公网；或 DNS 不通 | 配 `AUTOMIND_PROBE_HOSTS` 指向可达目标；`degraded` 时按 advice 查网关 / 本地服务 |
| 模型调不通（内网 relay） | relay 未启动 / 地址错 / 代理环境变量干扰 | `AUTOMIND_GATEWAY_PROBE=relay:443` 看 gateway 段；检查 `HTTPS_PROXY` / `NO_PROXY` |
| 服务端口不通 | 服务未启动 / 端口被占 / 绑错网卡 | `Get-NetTCPConnection -LocalPort 8765`；换端口 `--port`；确认 `--host 127.0.0.1` |
| `install.ps1` 中文乱码 / 解析报错 | 脚本被编辑器另存成无 BOM UTF-8 | 用原始脚本（UTF-8 BOM）；或 `git checkout` 恢复 |
| `install.ps1` 报"离线包目录里没有 .whl" | 拷贝了压缩包但没解压，或路径写错 | 解压后传目录；`-Package` 指向含 `.whl` 的目录 |
| `pip` 报 `No matching distribution` | 离线包缺依赖 | 联网机重新 `pip download "automind-agent[web]==1.11.14"` |
| 备份压缩失败（Permission denied / 文件被占用） | SQLite 正在被服务使用 | 停服务后备份（或维护窗口） |
| 回滚提示 SHA-256 失败 | 备份包损坏 / 被改过 | 换一个备份；重新从异地备份拷回 |
| `egress.log` 没有记录 | 审计被关（`AUTOMIND_EGRESS_AUDIT=0`）或尚无出站判定 | 打开审计并重启；跑一次出站工具制造记录 |
| `/api/health` 里 `network.state=unknown` | 首次探测尚未完成（后台刷新中） | 等 1–2 秒再查；离线模式下应快速变成 `expected_offline` |

---

## 9. 验收清单（交付 / 安全评审用）

在客户机器上逐项执行并留档（建议截图 / 保存 JSON 输出）：

- [ ] **版本一致**：`python -c "import automind; print(automind.__version__)"` → `1.11.14`
- [ ] **策略生效**：`python scripts\offline\smoke_offline.py --json` → `ok: true`，
      `policy.mode` 与约定一致；
- [ ] **三态正确**：离线环境 `network.state == expected_offline` 且 `public_skipped == true`；
      或 `net_check` 输出三段式结论（截图）；
- [ ] **公网出网被拒**：离线模式下 `http_request https://example.com` 返回失败，
      且错误信息说明"离线模式 / 怎么放行"，**不是**超时；
- [ ] **本地能力可用**：本地模型 / 内网 SearXNG / 内网 relay 正常（如部署了）；
- [ ] **出网审计**：`<数据目录>\logs\egress.log` 存在；离线 / 白名单模式下没有
      可疑公网放行记录；日志里没有 query 密钥；
- [ ] **无外联证据**：`verify.ps1` 通过；如客户要求，可在防火墙侧对 AutoMind 进程
      做连接审计（推荐双保险：应用层策略 + 网络层强制）；
- [ ] **入站为零**：`Get-NetTCPConnection -State Listen` 只有 `127.0.0.1` 上的回环监听，
      没有 `0.0.0.0`；
- [ ] **备份可回滚**：`backup.ps1` 成功、`rollback.ps1 -WhatIf` 演练通过；
- [ ] **一键验收**：`verify.ps1 -ExpectMode <约定模式>` → 全部 PASS、退出码 0。

统一入口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\offline\verify.ps1 -ExpectMode offline
```

---

## 10. 常见问题（FAQ）

**Q1：完全断网时商业版 Pro 能激活吗？**
能。许可证是 Ed25519 离线签名，客户机只带公钥；`.ampkg` 拖进应用内「一键升级」面板
即可装包 + 激活，全程不联网。旧 HMAC 格式不支持。详见 `docs/COMMERCIAL_ACTIVATION.md`。

**Q2：offline 模式下本地 Ollama / 内网 relay 还能用吗？**
能。策略只限制**公网**出网；回环 / 私网 / 内网域名默认放行。要连内网也管住，
设 `AUTOMIND_EGRESS_STRICT=1`。

**Q3：terminal 里的 `curl` / 第三方连接器 / MCP 会不会绕过白名单？**
会。应用层工具策略只能拦住"经 AutoMind 工具调用"的出网（官方工具、web 搜索、
浏览器、webhook），拦不住子进程、连接器与 MCP。所以**网络层必须再拦一次**
（正向代理域名白名单 / 防火墙 / DNS），这也是安全评审要求的两层设计。

**Q4：白名单支持端口通配吗？**
用 `host`（任意端口）或 `host:port`（指定端口）。不支持 `host:*`；需要限定端口就写端口。

**Q5：审计日志会一直变大吗？**
每次出站判定一行（约 200–300 字节）。日常量级很小；若长期高频使用，建议把
`<数据目录>\logs` 纳入日志轮转 / 清理策略（备份会包含该目录）。

**Q6：如何证明"真的没有外联"？**
三层证据：① `smoke_offline.py` / `verify.ps1` 的策略与探测结论；
② `egress.log` 里没有可疑公网放行；③ 网络层（防火墙 / 代理）对进程的出连接审计。
应用层不能替代网络层，向客户说明时务必两层都提。

**Q7：怎么停止 / 启动 / 开机自启？**
启动：`python -m automind.server --host 127.0.0.1 --port 8765`；
停止：结束监听 8765 的进程（`rollback.ps1` 里有同样逻辑）；
自启：`install.ps1 -RegisterAutostart`（HKCU Run，用户级，不需要管理员）。

**Q8：PowerShell 版本要求？**
Windows PowerShell **5.1** 即可，无需 PowerShell 7。脚本已做 5.1 兼容
（不用 `??` / 三元 / `-AsByteStream` 等 7.x 语法），并有语法解析测试守回归。

**Q9：多台机器 / 多用户如何部署？**
每台机器各自安装与策略配置；数据目录不要放网络共享（SQLite 锁语义不适合）。
集中管理可把 `egress-allow.txt` 由配置管理下发，但**仍需重启服务生效**。

**Q10：从 open 模式切到 offline 需要做什么？**
设置 `AUTOMIND_NETWORK_POLICY=offline`（或 `AUTOMIND_OFFLINE=1`）→ 重启服务 →
`verify.ps1 -ExpectMode offline`。日志会出现 `mode=offline` 的判定记录；
此前 open 模式下的历史放行记录会保留（`verify.ps1` 会识别为可疑并提示）。

---

## 11. 附录

### 11.1 环境变量总表

| 变量 | 默认 | 说明 |
|---|---|---|
| `AUTOMIND_OFFLINE` | 未设 | `1/true/yes/on` = offline（等价于 `NETWORK_POLICY=offline`） |
| `AUTOMIND_NETWORK_POLICY` | 未设 | `open` / `allowlist` / `offline`；优先级最高，非法值 fail-closed |
| `AUTOMIND_EGRESS_ALLOW` | 空 | 白名单（逗号 / 分号分隔） |
| `AUTOMIND_EGRESS_ALLOW_FILE` | 空 | 白名单文件（每行一条，# 注释） |
| `AUTOMIND_EGRESS_DENY` | 空 | 拒绝列表（优先级最高） |
| `AUTOMIND_EGRESS_AUDIT` | `1` | `0` 关闭出网审计 |
| `AUTOMIND_EGRESS_STRICT` | 未设 | `1` = 内网目标也必须显式白名单 |
| `AUTOMIND_PROBE_HOSTS` | 内置公网目标 | 公网探测覆盖，`host:port,...`（旧名 `AUTOMIND_NET_PROBE` 仍兼容，新名优先） |
| `AUTOMIND_GATEWAY_PROBE` | 空 | 网关 / relay 探测目标 `host:port` |
| `AUTOMIND_LOCAL_PROBES` | 空 | 本地服务探测目标，`host:port,...` |
| `AUTOMIND_DATA_DIR` | `%APPDATA%\AutoMind`（冻结版） | 数据目录 |
| `AUTOMIND_NET_MIRROR` | `1` | `0` 关闭 pip/npm 国内镜像改写 |
| `AUTOMIND_LICENSE_ALLOW_LEGACY_HMAC` | 未设 | 仅过渡期：`1` 临时接受 v1 HMAC 旧许可证（不建议） |

### 11.2 交付文件清单

```text
scripts\offline\install.ps1     离线安装 / 写策略 / 可选自启（幂等、失败回滚）
scripts\offline\backup.ps1      数据备份（zip + SHA-256 + manifest + 保留策略）
scripts\offline\rollback.ps1    数据回滚（校验 → 停服 → 备份当前 → 原子替换）
scripts\offline\verify.ps1      一键验收（版本 / 策略 / 三态 / 审计 / 端口）
scripts\offline\smoke_offline.py 冒烟自检（--json / --skip-network，可重复运行）
docs\OFFLINE_OPS.md             本手册
docs\OFFLINE_DEPLOYMENT.md      设计文档（第 5 节：已实现状态与锚点）
```

### 11.3 命令速查

```powershell
# 版本
python -c "import automind; print(automind.__version__)"
# 策略快照
python -c "from automind.core.offline import policy_status; import json; print(json.dumps(policy_status(), ensure_ascii=False, indent=2))"
# 三态
python -c "from automind.core.netresilience import network_state_sync; import json; print(json.dumps(network_state_sync(), ensure_ascii=False, indent=2))"
# 冒烟
python scripts\offline\smoke_offline.py --json
# 健康检查
Invoke-RestMethod http://127.0.0.1:8765/api/health | ConvertTo-Json -Depth 5
# 审计尾部
Get-Content <数据目录>\logs\egress.log -Tail 20
```
