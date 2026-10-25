# 商业激活闭环（v1.11.14，v1.11.15 起扩展为「商业运营体系」）

> 面向：**发行方**（置备密钥、批量发证、CRL 吊销、排障）与**客户 IT**（部署、换机、审查安全边界）。
> 目标只有一个：客户拿到一个文件、拖进升级面板 → 商业版真的激活；
> 且这条链路上的每一步都有命令可验证、有测试守回归。
>
> v1.11.15 增补（详见 §12 起）：许可证协议 v3（序列号 / 机器绑定 / 生效日）、
> 批量发证与台账、CRL 吊销、宽限期、周期复检与时钟回拨检测、公钥平滑轮换。
> 旧 v2 许可证与旧 `.ampkg` **完全兼容**，客户无需换证。

---

## 1. 闭环长什么样

```
发行方（一次性，本机）              发行方（每次发版，一条命令）                客户（1 分钟）
──────────────────────            ─────────────────────────────             ────────────────────
keygen --write-public-key    →    scripts/build_pro_upgrade.py        →     设置 →「升级专业版/企业版」
  · 私钥：pro/.license-private/     · 构建 wheel（版本 = 核心版本）             → 把 .ampkg 拖进「一键升级」
  · 公钥：keys.py 的 PUBLIC_KEY_HEX · doctor 自检（公钥/私钥/wheel/包）          → 看到「已升级」即完成
                                   · 签发许可证并打成 .ampkg（打印 sha256）      （若提示重启：重启一次自动激活）
```

`.ampkg` 本质是一个 zip：`automind-upgrade.json`（清单）+ `license.txt`（一行 `AMP2-…`）
+ `automind_pro-<ver>-py3-none-any.whl`。应用内 `automind.core.edition.install_upgrade()`
一步完成"解压装包 + 验签 + 写许可证 + 就地激活"。

---

## 2. 组件与边界（付费墙为什么成立）

| 组件（仓库内路径） | 作用 | 是否进 wheel / 进 git |
|---|---|---|
| `pro/automind_pro/licensing/keys.py` | **验签公钥**（主钥 + 平滑轮换的旧钥列表） | ✅ 进 wheel |
| `pro/automind_pro/licensing/ed25519.py`、`__init__.py` | 纯标准库 Ed25519 验签、许可证协议解析（v2 / v3） | ✅ 进 wheel |
| `pro/automind_pro/licensing/guard.py` | 客户端守卫：机器指纹 / CRL / 宽限 / 时钟回拨 / 周期复检 | ✅ 进 wheel |
| `pro/tools/issue_license.py` | 发行方离线工具：`keygen` / `issue` / `issue-batch` / `bundle` / `revoke` / `crl` / `machine` / `verify` / `pubkey` / `doctor` | ❌ `pyproject.toml` 显式 `exclude` + `MANIFEST.in prune` + CI 审计 |
| `pro/.license-private/private_key.hex` | **签发私钥**（32 字节种子 hex） | ❌ `.gitignore`（`pro/.license-private/`、`*private_key.hex`）+ `prune` + CI 审计 |
| `dist/upgrades/*.ampkg` | 发给客户的一键升级包（v1.11.15 起可随包附带 CRL） | ❌ `dist/` 在 `.gitignore` 内（走商业渠道分发） |
| `dist/licenses/*`、`dist/licenses/license_ledger.csv` | 批量发证产物与**台账**（客户/序列号/许可证/包 sha256） | ❌ 发行方内部留存，不进 git |
| `dist/crl/license_crl.json` | 吊销列表（CRL），随 `.ampkg` 分发 | ❌ 发行方内部维护，不进 git |

- 客户端**只有公钥**：能验签，不能签发。任何人拿到 wheel 都无法伪造/改档位/改到期日/改席位。
- **fail-closed**：`PUBLIC_KEY_HEX` 为 `"0" * 64` 时 `is_provisioned()` 为 False，
  `parse_license()` 一律返回 None —— "验不了"绝不等于"通过"。
- 环境变量 `AUTOMIND_LICENSE_PUBLIC_KEY` 优先于内置公钥（测试/应急换钥用；
  能改它的人本来就控制着那台机器）。
- v1 旧格式（`AMP-…`，对称 HMAC）默认拒绝：那套密钥历史上随包分发过，等同公开。

---

## 3. 发行方：一次性置备（换机器 / 换密钥时才重做）

```bash
# 1) 生成 Ed25519 密钥对；--write-public-key 直接把公钥写回 keys.py 并打印指纹
python pro/tools/issue_license.py keygen --write-public-key
# 输出 JSON：key_file / keys_file / public_key_hex / key_id（公开信息，可留档）

# 2) 确认置备生效
python pro/tools/issue_license.py pubkey          # provisioned=true，key_id 非空

# 3) 公钥变了必须重建 wheel（wheel 里内置验签公钥），然后自检
cd pro && python -m pip wheel . --no-deps --no-build-isolation -w dist && cd ..
python pro/tools/issue_license.py doctor
```

> ⚠️ **私钥纪律**
> - 只存在于 `pro/.license-private/private_key.hex`（权限收紧）或发行方密码库/加密介质；
> - **绝不提交、绝不进 wheel/sdist、绝不贴进文档 / 聊天 / 日志 / 截图**；
> - 丢失私钥：存量许可证继续有效，但无法再签新证（只能重新 keygen → 存量全部换发）；
> - 泄露私钥：立即 `keygen --force --write-public-key` 换钥、重建 wheel、重发所有客户证，
>   旧公钥签发的许可证全部作废。

`keygen` 的几种用法：

| 命令 | 行为 |
|---|---|
| `keygen` | 只生成私钥并打印公钥（需手工粘贴到 keys.py，不推荐） |
| `keygen --write-public-key` | 生成私钥 + **自动改写** `keys.py` 的 `PUBLIC_KEY_HEX` + 打印 key_id |
| `keygen --force --write-public-key` | 覆盖已有私钥（**已发许可证全部作废**，慎用） |
| `keygen --out <目录> --keys-file <keys.py>` | 自定义落点（测试/多钥匙场景） |

---

## 4. 发行方：每次发版（一条命令）

```bash
# 专业版（到期 2027-12-31）
python scripts/build_pro_upgrade.py --tier PRO --expiry 20271231 --customer acme

# 企业版（永久，50 席）
python scripts/build_pro_upgrade.py --tier ENT --expiry 00000000 --customer bigcorp --seats 50

# Windows PowerShell 等价写法
.\scripts\build_pro_upgrade.ps1 -Tier ENT -Expiry 00000000 -Customer bigcorp -Seats 50
```

脚本依次做（**任一步失败非零退出**）：

1. **版本一致性**：`automind/__init__.py` = `pro/automind_pro/__init__.py` = `pro/pyproject.toml`；
2. **构建 wheel**：`pro/dist/automind_pro-<ver>-py3-none-any.whl`（`pip wheel .` 在 `pro/` 内执行）；
3. **`doctor` 自检**（见 §5）；
4. **签发 + 打包** `.ampkg` 到 `dist/upgrades/`，打印许可证原文、sha256、客户升级步骤。

发版纪律：

- `pro/dist/` 只留**当前版本**的 wheel，`dist/upgrades/` 只留当前批次 `.ampkg`；
  旧 wheel 可能仍带占位公钥，旧 `.ampkg` 可能由已轮换的密钥签发 —— 两者都会被 `doctor` 点名；
- 台账字段：客户、档位、到期日、席位、序列号、机器指纹、许可证原文、`.ampkg` sha256、发出日期
  （v1.11.15 起由 `issue-batch --ledger` 自动维护，见 §13）；
- 批量发证统一走 `issue-batch`，不要再手工一条条 `issue` —— 手工操作既没有台账，
  也容易漏掉序列号，后续无法吊销；
- 不要手工 `keygen` 出新钥匙再出包（除非确实在换钥）—— 换钥等于作废存量许可证。
  平滑换钥请严格按 §18 的仪式执行。

---

## 5. `doctor`：发行前自检

```bash
python pro/tools/issue_license.py doctor
```

输出 JSON：`ok` + `checks[]`（每项 `name/ok/detail`）+ `warnings[]`；`ok=false` 时退出码 1。

| 检查项 | 失败意味着什么 | 怎么修 |
|---|---|---|
| `public_key_provisioned` | `keys.py` 公钥还是 `"0" * 64` 占位符 → 客户怎么填许可证都激活不了 | `keygen --write-public-key` |
| `private_key_available` | 找不到签发私钥 | `keygen` / `--key-file` / `AUTOMIND_LICENSE_PRIVATE_KEY` |
| `keypair_matches` | 私钥与内置公钥不是一对 → 签出来的证全部验不过 | 用配对的私钥，或重做 keygen + 重建 wheel |
| `core_package_version_match` | 商业包与主程序版本不同号 | 同步 `pro/automind_pro/__init__.py`、`pro/pyproject.toml` |
| `wheel_version_match` | `pro/dist` 里没有当前版本的 wheel | `cd pro && python -m pip wheel . --no-deps --no-build-isolation -w dist` |
| `wheel_public_key_matches` | **wheel 内置公钥 ≠ keys.py**（置备公钥后忘重建 wheel）→ 客户装上仍激活不了 | 重建 wheel 后重新 `bundle` |
| `sign_verify_roundtrip` | 本地私钥签出的探测许可证内置公钥验不过（实现/密钥异常） | 检查 licensing 代码与密钥 |
| `bundles_activatable` | 已有 `.ampkg` 的清单/许可证/wheel 版本/wheel 公钥有问题 | 删旧包、重新 `bundle` |

---

## 6. 客户：升级（1 分钟）

1. 打开 AutoMind → 左下角「设置」→「⭐ 升级专业版 / 企业版」（或点左上角版本徽标）；
2. 把 `.ampkg` 拖进「⚡ 一键升级」；
3. 看到「已升级为专业版 / 企业版」即完成；若提示重启（进程里已加载过旧扩展包），
   重启一次后自动激活（许可证已提前保存）。**不需要 pip、不需要改环境变量、不需要管理员。**

确认激活：界面左上角徽标；`GET /api/health` 的 `edition`（`community` / `pro` / `enterprise`）；
`GET /api/license` 给出 `step` / `reason_text` / `version_match` 等排障字段。

其它部署方式（运维批量 / 无界面服务器）：

- 分两步：面板拖入 `.whl` → 粘贴许可证（`AMP2-…` 一整行）；
- 命令行：`pip install automind_pro-<ver>-py3-none-any.whl`，把许可证写入
  `~/.automind/license`（或 `.automind_license` / `AUTOMIND_LICENSE`），重启服务；
- 接口：`POST /api/license/package`（请求体 = `.ampkg` 或 `.whl`），管理动作需管理员令牌。

---

## 7. 续期 / 升档 / 换扩展包

一律**再发一个新的 `.ampkg`**，客户拖进升级面板即可：

| 场景 | 发行方 | 客户 |
|---|---|---|
| 续期 | `bundle PRO <新到期日> acme` | 拖入新包 |
| 专业版 → 企业版 | `bundle ENT <到期日> acme` | 拖入新包（已在用的特性实例保留，业务不中断） |
| 扩展包发新版 | 重建 wheel（同号新构建）后 `bundle` | 拖入新包；界面提示重启一次后生效 |
| 换机换绑 | 用新机器指纹重新 `bundle ... --machine <新指纹>` | 拖入新包（旧证在旧机器上仍可用到到期/吊销） |

到期后自动降级社区版：服务照常启动、数据不丢，商业入口显示 🔒；续期包拖入立即恢复。
v1.11.15 起还支持**宽限期**（`AUTOMIND_LICENSE_GRACE_DAYS`，默认 0）：配置大于 0 时，
过期后仍可继续用 N 天但面板会标红提醒，给续费流程留出缓冲。详见 §14。

> 长驻进程注意：v1.11.15 之前，服务进程启动时校验一次后就"记住"了有效状态，
> 过期/吊销要重启才降级。现在周期复检（默认 3600 秒）会自动降级与恢复，详见 §17。

---

## 8. 排障速查

| 现象 | 先查什么 |
|---|---|
| 客户填了许可证仍是社区版 | 发行侧 `pubkey`（`provisioned: true`）+ `doctor` 全绿；客户装的是否同版本 wheel（旧 wheel 里可能是占位公钥） |
| 「许可证无效：签名校验未通过或已过期」 | 许可证是否由当前私钥签发、是否过期；`issue_license.py verify <许可证>` |
| 「许可证已保存但未能激活」 | 客户机是否设置了 `AUTOMIND_LICENSE` 或存在 `.automind_license`（优先于界面填写的证）；清除后重启 |
| 「扩展包协议版本不匹配」 | 客户主程序与扩展包 `EXTENSION_API_VERSION` 不一致；用同版本扩展包重新 `bundle` |
| 面板提示扩展包版本不一致 | `GET /api/license` 的 `version_match` / `version_reason`；发同号 `.ampkg` |
| 拖入后提示需重启 | 正常：进程已加载旧扩展（Python 模块不可热替换），重启一次；许可证已保存 |
| 发行包在客户机器上"能装不能激活" | 检查 wheel 内置公钥与 keys.py 是否一致（`doctor` 的 `wheel_public_key_matches`） |

---

## 9. 安全边界（给客户 IT 审查）

- 许可证校验**完全离线**（Ed25519 验签，纯标准库实现），不联网、不上传数据；
- 客户端包内**没有签发私钥**，也没有任何"本地生成许可证"的代码路径；
- 升级接口只接受 `automind_pro` 本身（zip 中出现其它顶层内容 / 路径穿越一律拒绝），
  不是"装任意 Python 包"的入口；
- 升级 / 激活是管理动作：本机可直接操作，远程需管理员令牌（`AUTOMIND_ADMIN_TOKEN`）；
- 私钥不进 git / wheel / sdist，由三处配置 + CI 审计保证（`tests/test_version_consistency.py`、
  `pro/tests/test_license_forgery.py`、`pro/tests/test_commercial_activation_pro.py`）。

---

## 10. 防回归测试（改动激活链路时必须跑）

```bash
# 激活/许可证/一键升级包全套（v1.11.15 起含协议 v3 / 批量发证 / CRL / 周期复检）
python -m pytest pro/tests/test_license_v3.py pro/tests/test_license_forgery.py \
                 pro/tests/test_license_and_activation.py pro/tests/test_in_app_upgrade.py \
                 pro/tests/test_issue_bundle.py pro/tests/test_commercial_activation_pro.py \
                 tests/test_commercial_activation.py -q

# 版本四处一致（社区核心 / 商业包 / pyproject / 安装器……）
python -m pytest tests/test_version_consistency.py tests/test_commercial_activation.py -q

# 发行工具与代码风格
python -m ruff check pro automind/core/edition.py tests
```

`pro/tests/test_commercial_activation_pro.py` 内容对应本闭环的四道保险：

1. 源码 `PUBLIC_KEY_HEX` 必须是合法 32 字节且非全 0；本机有发行私钥时还要求公钥与私钥配对；
2. `pro/dist/*.whl` 文件名版本必须等于 `automind_pro.__version__`（不许留旧 wheel）；
3. `.ampkg`（真实 wheel + 测试钥匙）走真实 `install_upgrade`：装包 → 许可证落盘 → 重启后
   edition/feature_flags 全对；另测仅许可证包就地激活；
4. `keygen --write-public-key` 真的改写 keys.py 且**不打印私钥**、`doctor` 对占位符/旧公钥 wheel
   必须非零退出、发行脚本版本读取与三处版本一致。
   （无私钥的 CI 环境自动 skip 需要私钥的用例，其余照跑。）

---

## 11. 发行状态快照（历史 / 当前）

> 说明：本节保留各版本出包时的**历史快照**；其中 **§11.5 为当前状态**，其余为历史快照。
> 公钥未轮换时 `PUBLIC_KEY_HEX` / `key_id` 两版一致；wheel / `.ampkg` 的版本号与
> sha256 以最新 `doctor` 输出为准，发行时同步更新本节与 `RELEASE.md`。

### 11.1 v1.11.14 历史快照（2026-10-21）

| 项目 | 值 |
|---|---|
| 验签公钥 `PUBLIC_KEY_HEX` | `7c285013074c1e8ee8b523956418b68bf92c84a49961b859443a727e06b99f63` |
| 公钥指纹 `key_id` | `e9fffafc0f6f19d4` |
| 商业包版本 | `1.11.14`（与社区核心同号） |
| wheel | `pro/dist/automind_pro-1.11.14-py3-none-any.whl`（95,700 B，sha256 `5ebb8a0e4586200a6222cf7de6e2192761dadf2a8c084cc283ca7cc59a7fe3d2`） |
| 专业版升级包 | `dist/upgrades/AutoMind-PRO-acme-20271231.ampkg`（94,288 B，sha256 `ed1e4e5bd06d3412408dd09c080adb3460e90167e47ba0ac1a20ff4be7847d47`） |
| 企业版升级包 | `dist/upgrades/AutoMind-ENT-bigcorp-00000000.ampkg`（94,308 B，sha256 `f869ea4f2249220f5d71e2b2f620196626d64bb0462887c3d884094d61d8c705`） |
| 端到端验证 | 隔离 HOME/数据目录下 `install_upgrade(.ampkg)`：PRO → `edition=pro`（19 项专业版特性全开、企业版专属全关）；ENT → `edition=enterprise`（27 项特性全开）；许可证落盘 `~/.automind/license`，`upgrade_status.step=active`、`version_match=true` |

### 11.2 v1.11.15 历史状态（2026-10-22）

| 项目 | 值 |
|---|---|
| 验签公钥 `PUBLIC_KEY_HEX` | `7c285013074c1e8ee8b523956418b68bf92c84a49961b859443a727e06b99f63`（未轮换） |
| 公钥指纹 `key_id` | `e9fffafc0f6f19d4`（无旧钥过渡） |
| 商业包版本 | `1.11.15`（与社区核心同号；内含 v3 许可证 / 机器绑定 / CRL / 周期复检） |
| wheel | `pro/dist/automind_pro-1.11.15-py3-none-any.whl`（114,399 B，sha256 `8c9c41daa666c41552958764900ee7c1fb475e69e75e5d4e0b0167b770b42f05`） |
| 专业版升级包 | `dist/upgrades/AutoMind-PRO-acme-20271231.ampkg`（113,079 B，sha256 `abef103be83552a9c76a5ac5eba02e308c5c7f6f43d03dd700aa9790067dbf69`） |
| 企业版升级包 | `dist/upgrades/AutoMind-ENT-bigcorp-00000000.ampkg`（113,089 B，sha256 `bb9bc22201ba416f41398302cb9655093a3668ba293f8dc9518a1764e4863edb`） |
| doctor | **12/12 全绿**（新增 key_rotation_state / crl_readable / license_recheck_config / machine_binding_ready） |
| 端到端验证 | 隔离 HOME/数据目录下 `install_upgrade(.ampkg)`：PRO → `edition=pro`（19/27 项特性全开）；ENT → `edition=enterprise`（27/27 全开）；`step=active`、`version_match=true`、许可证落盘 `~/.automind/license` |
| 前端 | `automind/static/dist/` 已按 v1.11.15 源码重建（tsc + vite build 通过） |

### 11.5 v1.11.18 当前状态（2026-10-25）

| 项目 | 值 |
|---|---|
| 验签公钥 `PUBLIC_KEY_HEX` | `7c285013074c1e8ee8b523956418b68bf92c84a49961b859443a727e06b99f63`（未轮换） |
| 公钥指纹 `key_id` | `e9fffafc0f6f19d4`（无旧钥过渡） |
| 商业包版本 | `1.11.18`（与社区核心同号；**本版无商业扩展包功能变更**，licensing 协议 / CLI / 扩展包接口与 v1.11.17 一致） |
| wheel | **已重建**：`pro/dist/automind_pro-1.11.18-py3-none-any.whl`（119,601 B，sha256 `f8b4fe284cf27ad37c6e806f545101e6c17f487b98e8377e5406997bc43fb04b`）；旧 1.11.17 wheel 已移除，避免 `doctor` 的 `stale_wheels` 报警。本版无商业功能变更，licensing 协议 / CLI / 扩展包接口与 v1.11.17 一致 |
| 一键升级包 `.ampkg` | **已重新出包（同号 1.11.18）**：`dist/upgrades/AutoMind-PRO-acme-20271231.ampkg`（118,303 B，sha256 `36ae82d09d825c4f1b6b222d8008488511c921273a0d80d0c1d3135c266594d3`）与 `AutoMind-ENT-bigcorp-00000000.ampkg`（118,313 B，sha256 `563310b156ad383834f67d139c81d566713745a43db480d4df56b063bf49bcfd`）；旧包已归档到 `dist/upgrades/archive-1.11.17/` |
| 端到端验证 | 本版未改商业特性与路由挂载，`version_match` 校验逻辑不变；`tests/test_commercial_activation.py` 全绿（含"dist wheel 必须与主程序同号"），`pro/tests` 全量通过，`doctor` **12/12 全绿**（含 2 个升级包可激活校验） |
| 前端 | 本版未改前端源码，`automind/static/dist/` 沿用 v1.11.17 构建产物 |

> ⚠️ v1.11.18 为**补丁发版**，改动集中在评测执行器无人值守审批、环境探测工作区口径、
> `file_write` 磁盘回读校验与后台任务 / 子智能体 / 团队共享任务工具；商业扩展包不需要随本版
> 重新出包，wheel 仅随核心同步版本号并重建。`pro/dist/` 里只保留当前版本的 wheel
> （历史 wheel 归档到 `pro/dist/archive-<ver>/`），否则 `doctor` 的 `stale_wheels` 会报警。

### 11.4 v1.11.17 历史状态（2026-10-23）

| 项目 | 值 |
|---|---|
| 验签公钥 `PUBLIC_KEY_HEX` | `7c285013074c1e8ee8b523956418b68bf92c84a49961b859443a727e06b99f63`（未轮换） |
| 公钥指纹 `key_id` | `e9fffafc0f6f19d4`（无旧钥过渡） |
| 商业包版本 | `1.11.17`（与社区核心同号；本版修复商业特性 REST 路由未挂载，licensing 协议与 CLI 与 v1.11.16 一致） |
| wheel | `pro/dist/automind_pro-1.11.17-py3-none-any.whl`（119,601 B，sha256 `620e509bea4df7b3b2a74cb7db8de1ba1c5029f03817d2f8dff1d9e113210ef2`） |
| 专业版升级包 | `dist/upgrades/AutoMind-PRO-acme-20271231.ampkg`（118,298 B，sha256 `829c311322788b5648d070d7394105faa3b02ac279f5554c05015edd1c5fe7b4`） |
| 企业版升级包 | `dist/upgrades/AutoMind-ENT-bigcorp-00000000.ampkg`（118,310 B，sha256 `1a4688b91b68efe2a5468fd09215ad93e4f1afc71bcf4ec26c2f9aaac8b9e39b`） |
| doctor | **12/12 全绿**（含 2 个升级包可激活校验） |
| 端到端验证 | PRO / ENT 许可证下 `GET /api/cache`、`GET /api/router` 与 ENT 的 `GET /api/costs`、`POST /api/kb/kbs` 均 **200**（此前全部命中 403 降级路由）；前端 dist 按 v1.11.17 源码重建 |
| 前端 | `automind/static/dist/` 已按 v1.11.17 源码重建（tsc + vite build） |
| 升级要求 | 建议**换发 v1.11.17 `.ampkg`**：本版修复"已激活专业版 / 企业版仍提示语义缓存/智能路由/成本仪表盘未开通"的路由挂载 bug；公钥未轮换，旧许可证仍有效，旧 `.ampkg` 仍可激活但缺少该修复 |

> ⚠️ v1.11.17 主要变更为商业特性路由挂载修复与工作区会话管理；
> `scripts/build_pro_upgrade.py` 的打包结果回显 bug 也已修复，一键发行闭环可用。
> 旧 wheel 请归档到 `pro/dist/archive-<ver>/`，否则 `doctor` 的 `stale_wheels` 会报警。

### 11.3 v1.11.16 历史状态（2026-10-23）

| 项目 | 值 |
|---|---|
| 验签公钥 `PUBLIC_KEY_HEX` | `7c285013074c1e8ee8b523956418b68bf92c84a49961b859443a727e06b99f63`（未轮换） |
| 公钥指纹 `key_id` | `e9fffafc0f6f19d4`（无旧钥过渡） |
| 商业包版本 | `1.11.16`（与社区核心同号；含 v3 许可证/机器绑定/CRL/周期复检，以及本轮协同·循环续跑等 Pro 侧变更） |
| wheel | `pro/dist/automind_pro-1.11.16-py3-none-any.whl`（119,586 B，sha256 `62d16f3fc72ececa5ac45db6857cd7850361a0cbc669efaeb2240a0b95cef53e`） |
| 专业版升级包 | `dist/upgrades/AutoMind-PRO-acme-20271231.ampkg`（118,282 B，sha256 `4bff67b29d15a2d7570c5a11216e4c2d71c38a5340ebba5dfab240104083084d`） |
| 企业版升级包 | `dist/upgrades/AutoMind-ENT-bigcorp-00000000.ampkg`（118,294 B，sha256 `66691c1adb9078a817e6bb79d6294b979f29a4f09306c057ef74c3494e85cc68`） |
| doctor | **12/12 全绿** |
| 端到端验证 | 隔离 HOME/数据目录下 `install_upgrade(.ampkg)`：PRO → `edition=pro`（19/27 项特性）；ENT → `edition=enterprise`（27/27 全开）；`step=active`、`version_match=true`、许可证落盘 `~/.automind/license` |
| 前端 | `automind/static/dist/` 已按 v1.11.16 源码重建（`index-CMqs_gvL.js` / `index-ZfBx0Oh2.css`） |
| 升级要求 | **旧 1.11.15 及更早 `.ampkg` 需换发 v1.11.16**：协同/循环模式的断点续跑会向 Pro 扩展传 `resume=` 参数，旧扩展包不支持；公钥未轮换，旧许可证仍有效 |

> ⚠️ v1.11.16 主要变更为任务执行连续性 / 断点续跑 / 工作流自动化 / 执行治理 / 路由成本页兼容性；
> licensing 协议与 CLI 与 v1.11.15 保持一致（Word 手册已升级到 v1.11.16）。


> 私钥不在本表、也不在仓库任何文件里 —— 上表所有内容都可以公开。
> 换钥后本节失效，须同步更新公钥/ key_id / wheel / `.ampkg` 的 sha256，并作废旧许可证。


---

# 第二部分：商业运营体系（v1.11.15）

> 📘 **配套图文操作手册**：由 `scripts/build_commercial_manual.py` 在发行方本机生成
> `docs/商业运营升级与发证操作手册_<版本>.docx`（65 页 / 11 张流程图与示意图，面向发行方运营与客服；
> 覆盖发证、打包、分发、激活、升级、续期、换机、吊销、台账与排障的逐步操作）。生成物与图表
> 资产体积较大且属于发行方交付材料，不随社区仓库分发；本文件是**技术参考**，Word 手册是**上手操作版**，
> 两者命令与默认值保持一致。

## 12. 商业运营总流程

v1.11.15 把"能签出一份许可证"补成"可以持续运营的一套流程"。全流程**离线**，
不依赖任何 SaaS；每一步都有命令可复现、有台账可追溯：

```
线索/签约 ──► 生成客户 CSV ──► issue-batch（逐行签发 + 台账 + 可选 .ampkg）
                                      │
                                      ▼
                          交付 .ampkg（一个文件：扩展包 + 许可证 + CRL）
                                      │
                                      ▼
                    客户在「设置 → 升级」拖入 → 激活（或粘贴许可证）
                                      │
              ┌───────────────────────┼───────────────────────┐
              ▼                       ▼                       ▼
        月度巡检（台账/到期）      续期（新 .ampkg）        退订/违约/换机
              │                       │                       │
              ▼                       ▼                       ▼
       到期提醒 → 宽限期 → 降级   新到期日/新机器指纹       revoke → CRL 随下一包分发
```

角色分工：

| 角色 | 职责 | 主要命令 |
|---|---|---|
| 发行运营 | 批量发证、维护台账、续期、吊销 | `issue-batch` / `bundle` / `revoke` / `crl` |
| 发行技术 | 密钥置备与轮换、wheel 构建、发布自检 | `keygen` / `doctor` / `pubkey` |
| 客户 IT | 部署、拖包激活、换机时提供本机指纹 | 升级面板 / `machine` 子命令 |
| 客户成功 | 到期前提醒、宽限期沟通、吊销解释 | 台账 + `verify` / `crl list` |

> 月度巡检 SOP、客服话术与应急预案见 **`docs/LICENSE_OPERATIONS.md`**。

---

## 13. 批量发证与台账

### 13.1 客户 CSV

推荐带表头；`#` 开头是注释行，空行忽略：

```csv
# 2026-10 批次：华东区续费
customer,tier,expiry,seats,machine,serial,start,notes,days
acme,PRO,,5,,ACME20260001,,续费一年,365
bigcorp,ENT,20271231,20,0123456789abcdef,ACME20260002,,绑定服务器,
huanqiu,PRO,,2,,ACME20260003,20261201,12月起生效,30
```

列说明：

| 列 | 必填 | 说明 |
|---|---|---|
| `customer` | 是 | 客户标识（字母数字/`._@-`，写入许可证前会归一） |
| `tier` | 是 | `PRO` / `ENT` |
| `expiry` | 二选一 | 到期日 `YYYYMMDD`；`00000000` = 永久 |
| `days` | 二选一 | 今天 + N 天到期（与 `expiry` 互斥，同时给会报该行失败） |
| `seats` | 否 | 席位；空/0 = 不限 |
| `machine` | 否 | 机器指纹 16 hex，或 `auto`（本机指纹，测试/演示用）；空 = 不绑定 |
| `serial` | 否 | 8~16 位字母数字；空 = 不发序列号（v2 证，吊销时用派生序列号） |
| `start` | 否 | 生效日 `YYYYMMDD`（not-before），空 = 立即生效 |
| `notes` | 否 | 备注（只进台账，不进许可证） |
| `issued` | 否 | 签发日（默认今天，仅排障/补录用） |

### 13.2 批量发证命令

```bash
python pro/tools/issue_license.py issue-batch \
    --csv dist/licenses/customers-202610.csv \
    --out dist/licenses/2026-10 \
    --bundles \
    --wheel pro/dist/automind_pro-1.11.15-py3-none-any.whl \
    --ledger dist/licenses/2026-10/license_ledger.csv \
    --crl dist/crl/license_crl.json
```

产物：

- `<out>/AutoMind-<TIER>-<customer>-<expiry>[-<serial>].license.txt`：每行一份许可证；
- `<out>/...ampkg`（加 `--bundles` 时）：每行一份一键升级包；
- `--ledger`：台账 CSV（追加写，不存在则先写表头）；
- stdout：JSON 摘要 `{"counts":{"ok":N,"failed":M},"licenses":[...],"failed":[...],"ledger":"...","bundles":N}`。

`--strict`：任一行失败时退出码 1（默认是"失败行汇总后继续"，适合大批量）；
失败原因逐行给出（tier 非法 / expiry 与 days 冲突 / seats 不是整数 …）。

### 13.3 台账字段

| 列 | 含义 |
|---|---|
| `customer` / `tier` / `expiry` / `seats` | 销售要素 |
| `issued` | 签发日 |
| `serial` | v3 序列号；v2 为派生序列号（`sha256(许可证)[:12]`，可直接吊销） |
| `machine` | 绑定指纹（空 = 未绑定） |
| `license` | 许可证原文（**发行方内部机密**：持有即可激活，只进内部台账） |
| `wheel` | 升级包文件名（未出包为空） |
| `sha256` | 交付物摘要：有 `.ampkg` 时是包 sha256，否则是许可证文本 sha256 |
| `notes` / `created_at` | 备注 / 记录时间 |

台账用途：续期提醒（扫 `expiry`）、客服定位（按 `serial` 查客户）、
退款/违约吊销（`serial` 直接 `revoke`）、交付物完整性核对（`sha256`）。
**台账含许可证原文，按商业机密管理**（`dist/` 已在 `.gitignore` 内）。

---

## 14. 续期与宽限期

### 14.1 续期

客户到期前，按同一 `serial`（或新 `serial`）签一份到期日更晚的许可证，
打成新的 `.ampkg` 让客户拖入即可 —— **不需要卸载/重装**，业务不中断：

```bash
python pro/tools/issue_license.py bundle PRO 20281231 acme \
    --serial ACME20260001 --machine 0123456789abcdef \
    --wheel pro/dist/automind_pro-1.11.15-py3-none-any.whl \
    --out dist/upgrades
```

### 14.2 提前发证（not-before）

续约常发生在旧证到期前。希望新证"到某天才生效、旧证到那天自动接上"时，
用 `--start` 签新证（v3 字段 `n`）：

```bash
python pro/tools/issue_license.py issue PRO 20281231 acme --start 20270101
```

未到生效日时：`parse_license` 返回无效、`parse_license_detail` 原因码
`not_yet_active`（含生效日），激活面板提示"许可证尚未生效"。

### 14.3 宽限期（客户端配置）

```bash
# 客户机/服务端环境变量（默认 0 = 过期立即失效；建议 7~30）
AUTOMIND_LICENSE_GRACE_DAYS=7
```

- 过期后 `≤ grace_days` 天：`check_license()` 返回 `valid=true, code=grace`，
  界面显示"宽限期剩余 N 天"，**不会**立刻降级；
- 超出宽限期：`code=grace_expired`，按过期处理（降级社区版）；
- 宽限只对"到期"生效，**不豁免吊销/机器不符/时钟回拨**；
- 服务端进程内由周期复检自动应用（见 §17）。

---

## 15. 机器绑定与换机

### 15.1 客户怎么拿到本机指纹

升级面板（设置 → ⭐ 升级与许可证）新增"本机信息"卡片：
显示 **本机指纹（16 位 hex）+ 复制按钮**，文案提示"购买需要绑定机器的许可证时，
把这段指纹发给发行方"。命令行等价：

```bash
python pro/tools/issue_license.py machine
# {"machine_id": "0123456789abcdef", "source": "hardware", ...}
```

指纹构成：`hostname + MAC/UUID + 平台 + 安装盐`，sha256 前 16 位；
安装盐在 `~/.automind/machine_salt`（重装/清用户目录会变）。
容器/镜像等每次指纹都会变的场景，可用环境变量固定：

```bash
AUTOMIND_MACHINE_ID=0123456789abcdef   # 测试/容器用；与许可证 m 字段比较
```

### 15.2 发绑定证

```bash
python pro/tools/issue_license.py bundle ENT 20271231 bigcorp \
    --machine 0123456789abcdef --serial BIGCORP0001 --seats 50
```

`--machine auto` = 用**发行方本机**指纹（只适合演示/单机测试，
给客户发证必须用客户提供的指纹）。

### 15.3 换机 / 迁移

| 场景 | 处理 |
|---|---|
| 客户换服务器 | 收集新机器指纹 → `bundle ... --machine <新指纹>` → 客户拖入新包；旧证留在旧机器到到期 |
| 旧机器已报废/被卖 | 先 `revoke <旧 serial>`（避免旧机继续用），再发新证 |
| 虚拟机克隆导致同指纹 | 机器绑定无法区分克隆体 —— 这类客户建议不绑定，改用席位/合同约束 |

绑定不符的表现：`parse_license` 无效；面板/`verify`/`activate_license` 报
`machine_mismatch`，错误信息里同时给出**本机指纹**与**许可证绑定指纹**，客服可直接比对。

---

## 16. CRL 吊销与分发

### 16.1 吊销

```bash
# 按许可证全文（v2/v3 都行；v2 自动派发序列号 sha256(license)[:12]）
python pro/tools/issue_license.py revoke AMP2-e9fffafc0f6f19d4-... --reason refund

# 按序列号（台账/客服沟通更方便）
python pro/tools/issue_license.py revoke ACME20260001 --reason "contract terminated"

# 查看
python pro/tools/issue_license.py crl list --crl dist/crl/license_crl.json
python pro/tools/issue_license.py crl show --crl dist/crl/license_crl.json
```

CRL 默认路径 `dist/crl/license_crl.json`（发行侧），写入是**原子追加 + 按 serial 去重**；
重复吊销保留首次的 reason/time，不会覆盖。

CRL 格式（客户端两种都接受）：

```json
{"revoked": [
  {"serial": "ACME20260001", "reason": "refund", "time": "2026-10-22T10:00:00"},
  {"serial": "0123456789ab"}
]}
```

### 16.2 分发

客户端读取顺序：`AUTOMIND_LICENSE_CRL` 环境变量 → `~/.automind/license_crl.json`。

分发方式优先级：

1. **随 `.ampkg` 分发（推荐）**：`bundle` 会自动把存在的 CRL 打进包
   （`license_crl.json` + manifest `crl` 摘要）；客户拖入升级包时，安装器**先合并 CRL
   再激活** —— 被吊销的证当场装不上；
2. 手工下发 `license_crl.json` 到客户机 `~/.automind/`（或配置环境变量指向共享路径）；
3. 运维批量替换（内网共享目录 + 计划任务），见 `docs/LICENSE_OPERATIONS.md`。

限制与注意：

- CRL 是**离线**列表：签了吊销但没分发出去，客户机不会知道 —— 撤销必须伴随分发；
- `bundle` 会拒绝"把已吊销的许可证打进包"；
- `doctor` 的 `bundles_activatable` 会解析每个 `.ampkg` 的随包 CRL 并核对许可证；
  旧包（无 CRL）视为通过；
- 需要"恢复"某客户时手工从 CRL 删除该条并重新分发（工具不提供 un-revoke）。

---

## 17. 周期复检与时钟回拨

### 17.1 行为

| 配置 | 默认 | 说明 |
|---|---|---|
| `AUTOMIND_LICENSE_RECHECK` | `3600`（秒） | 周期复检间隔；`0` = 关闭（不推荐，长驻进程会退回"启动后不再检查"） |
| `AUTOMIND_LICENSE_GRACE_DAYS` | `0` | 宽限天数（见 §14） |

激活成功后，扩展包启动**唯一**守护线程（`automind-license-watchdog`）：

- **有效 → 无效**（过期 / 吊销 / 机器不符 / 时钟回拨 / 超出宽限）：
  把当前进程降级为社区版（清特性、`edition=community`），
  **保留**扩展包安装状态与 `~/.automind/license` 许可证文件，
  并把原因记录到 `edition._state["license_reason"]`（面板显示"为什么被降级"）；
- **无效 → 有效**（客户续期、修正时钟、CRL 修正后）：自动重新激活，无需重启；
- 状态变化通过 `register_license_change_listener(fn)` 通知宿主（服务端据此广播前端）。

服务端集成（**由 Lead 在 `automind/server.py` 完成**，本模块不改 server）：

```python
# 位置建议：_broadcast_edition_change() 定义之后（同文件末尾附近）
_MAIN_LOOP: "asyncio.AbstractEventLoop | None" = None

def _on_license_change(event: dict) -> None:
    """周期复检线程回调：投递回主事件循环广播 edition 变化。"""
    loop = _MAIN_LOOP
    if loop is None or loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(_broadcast_edition_change(), loop)
    except RuntimeError:
        pass

def _wire_license_watchdog() -> None:
    try:
        from automind_pro.licensing.guard import register_license_change_listener
    except Exception:
        return
    register_license_change_listener(_on_license_change)
```

在 lifespan 启动处（`_attach_extensions()` 之前/之后均可）设置：
`global _MAIN_LOOP; _MAIN_LOOP = asyncio.get_running_loop(); _wire_license_watchdog()`。

### 17.2 时钟回拨

`~/.automind/license_state.json` 记录每次检查的 `last_seen/machine/serial`。
若系统时间比 `last_seen` **回拨超过 1 天**：判定 `clock_rollback` → 无效并降级
（不静默续命）；回拨发生时**不会**把 `last_seen` 改小（否则回拨会被合法化）。
±1 天内的 NTP 校正不误伤。修正时钟后，下一次复检自动恢复。

> 注意：把时钟**向前**调不会触发回拨检测，但会让许可证"提前到期"——
> 这是离线许可证的固有代价（正向不可信）；客户改回正确时间即可恢复。

---

## 18. 公钥轮换仪式（平滑换钥）

单公钥的换钥等于"一夜之间所有存量许可证失效"。v1.11.15 用
**主钥 + 旧钥列表**解决：新证用新钥签，旧证在过渡期仍被认可。

### 18.1 正常轮换（推荐，一年一次）

```bash
# 0) 前置：doctor 全绿；旧私钥先备份到离线密码库（本工具不会替你复制私钥！）
python pro/tools/issue_license.py doctor

# 1) 平滑换钥：新私钥写入默认私钥文件；旧主钥自动移入 LEGACY_PUBLIC_KEYS_HEX，
#    并写入预计下线日（默认今天+365 天，可 --retire-after 指定）
python pro/tools/issue_license.py keygen --rotate
# → JSON: key_id / old_key_id / legacy_key_ids / retire_after

# 2) 重建 wheel（wheel 内置公钥 + 旧钥列表），再跑 doctor：
#    key_rotation_state 应显示"过渡期：N 把旧钥仍被认可，预计下线日 ..."
cd pro && python -m pip wheel . --no-deps --no-build-isolation -w dist && cd ..
python pro/tools/issue_license.py doctor

# 3) 之后签发的新证自动用新私钥；旧证继续有效（客户端按 KID 选钥）
python pro/tools/issue_license.py bundle PRO 20281231 acme

# 4) 过渡期内完成存量客户换证；不晚于 retire_after：
#    把 LEGACY_PUBLIC_KEYS_HEX 清空（LEGACY_KEYS_RETIRE_AFTER 同步清空）→ 重建 wheel → doctor
```

换钥后必须同步更新 `docs/COMMERCIAL_ACTIVATION.md` §11 的公钥 / key_id / wheel / `.ampkg` sha256。

### 18.2 私钥泄露的紧急轮换（不等过渡期）

泄露 = 存量全部作废。此时**不能**把旧钥留在 LEGACY（那等于继续认可泄露钥签的证）：

```bash
python pro/tools/issue_license.py keygen --force --write-public-key   # 直接替换主钥，清空旧钥列表
cd pro && python -m pip wheel . --no-deps --no-build-isolation -w dist && cd ..
python pro/tools/issue_license.py doctor                              # 全绿（key_rotation_state=未轮换）
# 重新签发全部客户证；旧证立即失效
```

### 18.3 节奏与纪律

- 正常轮换一年一次，过渡期建议 6~12 个月；`retire_after` 过期后 `doctor` 会失败
  （强制你完成"删旧钥 + 重建 wheel"这一步，避免旧钥无限期有效）；
- 旧私钥在轮换后不再需要（旧证签名已存在），但**备份旧私钥仅用于审计**；
- 私钥永远只在 `pro/.license-private/private_key.hex` / 离线密码库，
  不打印、不复制到仓库/聊天/工单/截图。

---

## 19. 新版命令速查（v1.11.15）

```bash
# 机器指纹（做绑定证前先问客户要这段）
python pro/tools/issue_license.py machine

# 签发：expiry 或 --days 二选一；--serial/--machine/--start 触发协议 v3
python pro/tools/issue_license.py issue PRO 20271231 acme
python pro/tools/issue_license.py issue PRO acme --days 365 --serial ACME20260001
python pro/tools/issue_license.py issue ENT acme --days 30 --machine auto
python pro/tools/issue_license.py issue PRO acme --days 365 --start 20270101

# 一键升级包：可自动附带 CRL；--machine 绑定；--out 指定目录
python pro/tools/issue_license.py bundle PRO 20271231 acme --machine 0123456789abcdef
python pro/tools/issue_license.py bundle ENT 00000000 bigcorp --seats 50 --out dist/upgrades

# 批量发证 + 台账 + 升级包
python pro/tools/issue_license.py issue-batch --csv customers.csv --out dist/licenses \
    --bundles --wheel pro/dist/automind_pro-<ver>-py3-none-any.whl --strict

# 吊销 / 查 CRL
python pro/tools/issue_license.py revoke AMP2-... --reason refund
python pro/tools/issue_license.py revoke ACME20260001 --reason refund --crl dist/crl/license_crl.json
python pro/tools/issue_license.py crl list
python pro/tools/issue_license.py crl show

# 校验（v3 字段 + 序列号 + 机器指纹对照）
python pro/tools/issue_license.py verify AMP2-...

# 发行前自检（公钥/私钥/wheel/升级包/CRL/轮换/复检/机器绑定；失败非零退出）
python pro/tools/issue_license.py doctor
```

`issue` / `bundle` 的位置参数变化（向后兼容）：

- 旧写法 `issue PRO 20271231 acme` 不变；
- 新写法 `issue PRO acme --days 365`（第二个位置参数是客户名）；
- 同时给位置 expiry 和 `--days` → 报错退出（二者冲突）。

---

## 20. v2 兼容与迁移说明

| 项目 | v2（历史） | v3（v1.11.15+） |
|---|---|---|
| payload | `{"v":2,"t","e","c","i","s"}` | `{"v":3,...,"id","m","n"}`（三个均可选） |
| 序列号 | 无（用 `sha256(许可证)[:12]` 派生） | `id`：8~16 位字母数字，可读、便于客服沟通 |
| 机器绑定 | 不支持 | `m`：sha256 前 16 位 hex |
| 生效日 | 不支持 | `n`：YYYYMMDD（not-before） |
| 解析 | 继续支持（`LicenseInfo.version == 2`） | 新增字段，`summary()` 增补 serial/machine/not_before |

兼容纪律：

- `parse_license(raw)` 契约不变：无效/过期/未生效/机器不符/被吊销一律 `None`，
  旧调用点零改动；需要原因时用新增的 `parse_license_detail(raw)`；
- 工具**不给** `--serial/--machine/--start` 时仍输出 v2 许可证（历史行为不变）；
- **旧扩展包（v1.11.14 及更早）不认识 v3**：给还没升级客户端的客户发证时，
  应发 v2（不带上述三个参数），或让 `.ampkg` 同时升级扩展包（推荐，
  包内 wheel 已是新版）；
- 旧 `.ampkg`（无 CRL、v2 许可证）在 v1.11.15 客户端上继续可激活；
- 已在用的 v2 许可证无需换发，到期续期时再升级到 v3 即可。

### 20.1 关键数据结构（服务端/面板契约）

`parse_license_detail(raw) -> (LicenseInfo | None, reason_code, detail)`，原因码：

| 原因码 | 含义 | 面板文案（摘要） |
|---|---|---|
| `ok` | 有效 | 许可证有效 |
| `empty` | 未配置 | 还没有配置许可证 |
| `signature_invalid` | 验签失败 | 内容被改动或不是本发行方签发 |
| `key_id_unknown` | 不认识的 KID | 需要升级扩展包（轮换过渡） |
| `payload_invalid` | 格式非法 | 许可证内容格式不合法 |
| `expired` | 已过期 | 许可证已过期，请续期 |
| `not_yet_active` | 未到生效日 | 尚未到生效日（含日期） |
| `machine_mismatch` | 机器不符 | 绑定机器与本机不符（含双方指纹） |
| `revoked` | 已被吊销 | 已被吊销（含序列号/原因/时间） |
| `clock_rollback` | 时钟回拨 | 校正系统时间后重试 |
| `grace_expired` | 超出宽限 | 已过期且超出宽限期 |
| `grace` | 宽限期内 | 已过期，处于宽限期 |
| `public_key_not_provisioned` | 无公钥 | 安装包不完整，重新获取 |
| `legacy_hmac_disabled` | 旧 v1 证 | 已停用，请换发 AMP2 |

`license_status()` / `edition.upgrade_status()` 新增字段（旧字段全部保留）：
`machine_id` / `license_serial` / `machine_bound` / `grace_days` / `grace_left` /
`grace` / `recheck_seconds` / `crl_path` / `crl_revoked_count` / `clock_ok` /
`last_check` / `reason_code` / `detail` / `license_state`（被降级的原因）。

### 20.2 开发/默认环境值

| 项 | 默认 | 覆盖方式 |
|---|---|---|
| 宽限天数 | 0 | `AUTOMIND_LICENSE_GRACE_DAYS` |
| 复检间隔 | 3600 秒 | `AUTOMIND_LICENSE_RECHECK`（0=关闭，测试用 0 或很小的小数） |
| CRL 路径 | `~/.automind/license_crl.json` | `AUTOMIND_LICENSE_CRL` |
| 时钟状态 | `~/.automind/license_state.json` | 无（随 HOME 隔离） |
| 机器盐 | `~/.automind/machine_salt` | 测试用 `AUTOMIND_MACHINE_ID` 固定指纹 |
| 机器指纹 | 硬件派生 16 hex | `AUTOMIND_MACHINE_ID`（任意字符串会归一成 16 hex） |
| 发行侧 CRL/台账 | `dist/crl/`、`dist/licenses/` | `--crl` / `--ledger` |
| 时钟回拨容忍 | 86400 秒（1 天） | 代码常量 `guard.CLOCK_ROLLBACK_TOLERANCE` |

> 测试环境不要真的起 3600s 复检线程：`pro/tests/conftest.py` 已统一
> `AUTOMIND_LICENSE_RECHECK=0`，需要验证 watchdog 的用例会临时改成很短的小数间隔。
