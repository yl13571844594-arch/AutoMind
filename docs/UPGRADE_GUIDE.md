# 专业版 / 企业版升级方案（v1.11.14）

> 目标：**客户只做一件事 —— 把一个文件拖进升级面板。** 不用命令行、不用 pip、不用改环境变量、不用重启。
>
> 发行方也只做一件事：每个客户跑**一条命令**，得到一个 `.ampkg` 升级包发给客户。
>
> 完整闭环（密钥纪律 / `doctor` 检查项 / 排障速查 / 当前发行状态）见
> [docs/COMMERCIAL_ACTIVATION.md](COMMERCIAL_ACTIVATION.md)。

---

## 一图看懂

```
发行方（一次性）                 发行方（每个客户）                     客户
─────────────────               ─────────────────────                ──────────────────────
① 生成签名密钥对                 ③ 一条命令出升级包：                  ⑤ 设置 →「⭐ 升级专业版/企业版」
② 公钥写进扩展包并构建              issue_license.py bundle ...         ⑥ 把 .ampkg 拖进「⚡ 一键升级」
                                 ④ 把 .ampkg 发给客户（邮件/网盘/IM）  ⑦ 看到「🎉 已升级为…」，完成
```

`.ampkg` = 扩展包（`automind_pro-x.y.z.whl`）+ 许可证（一行）+ 清单，打在一个文件里。
许可证是**离线 Ed25519 签名**：客户机器不联网也能激活，客户手里没有私钥，无法伪造或改档位、改到期日。

---

## 一、发行方：一次性准备（约 5 分钟，只做一次）

> ✅ **v1.11.14 起本仓库已完成这一步**：内置验签公钥已置备
> （`python pro/tools/issue_license.py pubkey` → `"provisioned": true`，
> key_id `e9fffafc0f6f19d4`），Pro wheel 也已用该公钥重建。
> 只有**换机器/换发行密钥**时才需要重做本节。

### 1. 生成签名密钥对（公钥自动写回 keys.py）

```bash
python pro/tools/issue_license.py keygen --write-public-key
```

- 私钥写到 `pro/.license-private/private_key.hex`（已在 `.gitignore`，**不要提交、不要发给任何人**）；
- `--write-public-key` 会把公钥**直接写进**
  `pro/automind_pro/licensing/keys.py` 的 `PUBLIC_KEY_HEX`（不用再手抄；
  手抄漏一次就退化成占位符 → 客户怎么填许可证都激活不了）；
- 输出 JSON 里的 `public_key_hex` / `key_id` 可公开留档；**私钥内容不会打印**。

**私钥务必备份**（加密 U 盘 / 密码管理器）。丢了私钥只能重新 keygen，而旧密钥签的许可证会随新公钥全部失效。
也可以不落盘：把私钥放进环境变量 `AUTOMIND_LICENSE_PRIVATE_KEY`（64 位 hex）。

### 2. 重建 wheel 并自检

```bash
# 重新构建扩展包（公钥变了 / 发新版本时都必须重建，wheel 里内置公钥）
cd pro && python -m pip wheel . --no-deps --no-build-isolation -w dist && cd ..

# 发行前自检：公钥/私钥配对、wheel 版本与内置公钥、已有 .ampkg 可激活
python pro/tools/issue_license.py doctor

python pro/tools/issue_license.py pubkey      # 确认 "provisioned": true
```

公钥可以公开，放进分发包没有任何风险；**扩展包每次发新版本都要用同一把公钥构建**。
`doctor` 会特意检查 "wheel 内置公钥 == keys.py 公钥" —— 置备公钥后忘记重建 wheel，
是本项目历史上真实发生过的翻车点。

---

## 二、发行方：给客户出升级包（每个客户一条命令）

推荐用一键发行脚本（自动完成 构建 wheel → 版本校验 → doctor → 出包 → 打印 sha256）：

```bash
python scripts/build_pro_upgrade.py --tier PRO --expiry 20271231 --customer acme
python scripts/build_pro_upgrade.py --tier ENT --expiry 00000000 --customer bigcorp --seats 50

# Windows PowerShell 等价写法
.\scripts\build_pro_upgrade.ps1 -Tier ENT -Expiry 00000000 -Customer bigcorp -Seats 50
```

也可以直接调用签发工具（wheel 已构建好时最省事）：

```bash
# 专业版，到 2027-12-31
python pro/tools/issue_license.py bundle PRO 20271231 acme

# 企业版，永久，50 席
python pro/tools/issue_license.py bundle ENT 00000000 bigcorp --seats 50
```

输出 `dist/upgrades/AutoMind-PRO-acme-20271231.ampkg`，同时在屏幕上打印许可证原文（留档用）。

| 参数 | 说明 |
|---|---|
| `PRO` / `ENT` | 专业版 / 企业版（企业版包含专业版全部功能） |
| `YYYYMMDD` | 到期日；`00000000` = 永久 |
| `客户标识` | 字母数字，不含连字符（出现在许可证里，排障时用） |
| `--seats N` | 席位数，0 = 不限 |
| `--wheel 路径` | 指定扩展包；默认取 `pro/dist` 里与当前版本一致的，没有就现场构建 |
| `--out 目录` | 输出目录，默认 `dist/upgrades/` |

**出包前会自检**（`doctor` 同样检查，发行流程建议每次都跑）：

1. 用内置公钥把刚签的许可证验一遍，私钥与公钥不是一对时直接拒绝出包；
2. wheel 里**内置的验签公钥**必须与 `keys.py` 一致 —— 置备公钥后忘记重建 wheel，
   客户装上后会用旧公钥验签，照样激活不了；
3. wheel 文件名版本必须等于商业包版本（pro/dist 里旧版本 wheel 会被点名，先清理再发版）。

任一项不满足都会非零退出：不会发出一个"客户怎么也激活不了"的升级包。
建议留一张台账：客户、档位、到期日、席位、许可证原文、`.ampkg` 的 sha256、发出日期。

---

## 三、客户：升级（1 分钟）

1. 打开 AutoMind → 左下角「设置」→「⭐ 升级专业版 / 企业版」（或点左上角版本徽标）；
2. 把收到的 `.ampkg` 文件拖进「⚡ 一键升级」框；
3. 看到「🎉 已升级为专业版 / 企业版」即完成 —— **新功能当场可用，无需重启**。

就这样。没有第 4 步。

<details>
<summary>其它方式（运维批量部署、无界面服务器）</summary>

- **分两步**：升级面板里先拖入 `.whl` 扩展包，再粘贴许可证（一整行，以 `AMP2-` 开头；
  v1 旧格式 `AMP-` 默认拒绝，见 RELEASE.md「许可证签发」）；
- **命令行**：`pip install automind_pro-*.whl`，再把许可证写进 `~/.automind/license`
  或设置环境变量 `AUTOMIND_LICENSE=AMP2-…`，重启服务；
- **接口**：`POST /api/license/package`（请求体 = `.ampkg` 或 `.whl` 文件内容）。
  这是管理动作：本机直接可用，远程调用需带管理员令牌。

</details>

### 续期、升档、更新扩展包

一律是**再发一个新的 `.ampkg`**，客户拖进升级面板的「🔁 续期 / 升级到更高版本」即可：

| 场景 | 发行方 | 客户 |
|---|---|---|
| 续期 | `bundle PRO <新到期日> acme` | 拖入新包 |
| 专业版 → 企业版 | `bundle ENT <到期日> acme` | 拖入新包（已在用的功能不中断） |
| 扩展包发新版 | 构建新 wheel 后重新 `bundle` | 拖入新包；因为旧版扩展已在运行，界面提示**重启一次**后生效（许可证已提前保存） |

### 到期会怎样

到期后**自动降级为社区版**，服务照常启动、数据不丢；商业功能入口显示 🔒。续期包拖进去立即恢复。

---

## 四、常见问题

| 现象 | 原因与处理 |
|---|---|
| 「许可证无效：签名校验未通过或已过期」 | 发行方构建扩展包时没填公钥 / 用了另一把密钥；或许可证已过期。发行方用 `issue_license.py verify <许可证>` 自查 |
| 「许可证已保存但未能激活」 | 客户机器上设置了环境变量 `AUTOMIND_LICENSE`，或当前目录有 `.automind_license`，它们优先于界面里的许可证。清除后重启 |
| 「扩展包协议版本不匹配」 | 扩展包与社区版主程序版本不配套：发行方用与客户主程序同版本的扩展包重新 `bundle`（先跑 `issue_license.py doctor`） |
| 升级面板提示「扩展包版本与主程序不一致」 | 客户装的是旧版 wheel：发一个新版本 `.ampkg`（wheel 与主程序同号），客户拖入即可；`GET /api/license` 的 `version_match` / `version_reason` 会给出说明 |
| 客户填对许可证但仍显示社区版 | 发行侧先自查：`python pro/tools/issue_license.py pubkey`（必须 `provisioned: true`）+ `doctor` 全绿；再确认客户装的是同一版本 wheel（旧 wheel 里可能是占位公钥） |
| 拖入后提示需要重启 | 进程里已经加载过旧版扩展包（Python 模块不能热替换），重启一次即可，许可证已保存 |
| 想确认当前版本 | 界面左上角徽标；`GET /api/health` 的 `edition` 字段（`community` / `pro` / `enterprise`）；`GET /api/license` 含版本匹配与卡点信息 |

## 五、安全边界（给客户 IT 审查用）

- 许可证校验**完全离线**，不联网、不上传任何数据；
- 客户端只有验签公钥，没有签发私钥 —— 无法伪造、无法改档位或到期日；
- 升级接口只接受 `automind_pro` 本身（压缩包里出现别的顶层内容一律拒绝），不是"装任意 Python 包"的入口；
- 升级 / 激活是管理动作：本机可直接操作，远程操作需要管理员令牌（`AUTOMIND_ADMIN_TOKEN`）。
