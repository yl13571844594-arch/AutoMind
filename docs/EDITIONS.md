# AutoMind 版本说明 — 社区版 / 专业版 / 企业版

自 **v0.6.0** 起，AutoMind 采用「开源核心（Open-Core）」模式：

- **社区版（Community）** — 本仓库 `automind/` 目录，MIT 许可，永久免费开源；
- **专业版（Pro）/ 企业版（Enterprise）** — 商业扩展包 `automind-pro`（闭源，
  独立分发），安装并配置许可证后在运行时自动激活，社区核心零改动。

## 1. 功能对比

| 类别 | 功能 | 社区版 | 专业版 | 企业版 |
|------|------|:---:|:---:|:---:|
| 交互模式 | 💬 对话（流式、多模态、语音） | ✅ | ✅ | ✅ |
| | ⚙️ 工作（分层规划 + 符号验证） | ✅ | ✅ | ✅ |
| | 💻 编程（ReAct + TDD 内环） | ✅ | ✅ | ✅ |
| | 🤝 协同（多智能体编排） | — | ✅ | ✅ |
| | 🔁 循环（Loop Engineering） | — | ✅ | ✅ |
| 能力底座 | 工具 / 技能 / MCP / 插件 | ✅ | ✅ | ✅ |
| | 记忆（短期/长期/知识图谱） | ✅ | ✅ | ✅ |
| | 反思 / 自我纠错 / 自主任务闭环 | ✅ | ✅ | ✅ |
| 安全 | 工具审批（deny>ask>allow）· 安全审计 | ✅ | ✅ | ✅ |
| | 访问鉴权 · 限流 · 密钥脱敏 · CORS | ✅ | ✅ | ✅ |
| 运营 | 基础统计（/api/stats、Token 用量） | ✅ | ✅ | ✅ |
| | 📊 高级统计仪表盘（命中率/效率/趋势） | — | ✅ | ✅ |
| | ⏰ 定时任务 | — | ✅ | ✅ |
| 多用户 | 对话历史按会话隔离 | ✅ | ✅ | ✅ |
| | 👥 会话级 Agent 池（执行态隔离） | — | — | ✅ |
| | 独立统计分析服务（analytics-service） | — | — | ✅ |

> 设计原则：**安全不设付费墙** — 鉴权、限流、脱敏、审计全部保留在社区版。

## 2. 安装与激活

```bash
# 社区版（默认）
pip install -e ".[web]"
python -m automind.server --port 8765

# 专业版 / 企业版
pip install automind-pro          # 商业渠道获取的 wheel（或源码目录 ./pro）
set AUTOMIND_LICENSE=AMP-PRO-...  # 或写入 .automind_license / ~/.automind/license
python -m automind.server --port 8765
```

激活结果可通过 `GET /api/health` 的 `edition` 字段确认
（`community` / `pro` / `enterprise`）；Web 界面左上角也会显示对应徽标。

- 许可证无效或过期时**自动降级为社区版**，服务照常启动，不会报错退出。
- 社区版调用商业端点（如 `/api/schedule`、`/api/stats/detail`）返回
  `403 + 升级提示`；Web 界面对应入口显示 🔒。

## 3. 技术架构：如何做到互不影响

```
automind/            ← 社区核心（MIT 开源）
  core/edition.py    ← 稳定扩展协议 v1：特性注册表 + 扩展加载器
  core/prompts.py    ← 双方共用的角色提示词（稳定资源）
  agent.py           ← run_multi/run_loop 仅做特性门控与委派
  server.py          ← 商业路由由扩展注册；社区版注册 403 降级路由

pro/automind_pro/    ← 商业扩展（闭源，独立打包）
  license.py         ← 离线许可证校验（HMAC 签名）
  multiagent/        ← 协同模式编排器
  loop_engine.py     ← 循环工程引擎
  scheduler.py       ← 定时任务（路由 + 后台调度）
  stats_pro.py       ← 高级统计仪表盘路由
  session_pool.py    ← 企业版会话池
```

约束与承诺：

1. **单向依赖**：商业包依赖社区核心，社区核心从不 import 商业包
   （仅在运行时按名探测 `automind_pro`）。
2. **稳定契约**：商业包只使用 `edition.py` 文档中标注为
   *扩展契约 v1* 的接口（特性注册表、server_ctx 键、Agent 执行原语、
   `core/prompts.py`）。社区版任意重构只要保持这些契约不变，
   已发布的专业版无需改动即可继续工作。
3. **版本协商**：双方各自声明 `EXTENSION_API_VERSION`；不一致时扩展
   拒绝加载并记录日志，绝不半激活。
4. **打包隔离**：社区版 `pyproject.toml` 只收 `automind*`；`pro/` 目录
   有独立的 `pyproject.toml` 与商业许可证（LICENSE-COMMERCIAL.md），
   开源发布脚本（`scripts/build_community.py`）会强制校验产物中
   不含任何商业代码。

## 4. 许可证格式（商业版）

```
AMP-<TIER>-<EXPIRY>-<CUSTOMER>-<SIG>
     PRO|ENT  YYYYMMDD    客户标识   HMAC-SHA256 前 20 位
              (00000000=永久)
```

发行方生成：`python -m automind_pro.license PRO 20271231 acme`

## 5. 常见问题

- **社区版会被削功能吗？** 不会。上表社区版列的所有功能承诺保持开源，
  后续新增的基础能力也进入社区版。
- **从社区版升级要迁移数据吗？** 不需要。配置（`.automind_config.json`）、
  对话历史（`.automind/`）、记忆数据完全共用。
- **专业版影响社区版更新吗？** 不影响。扩展协议向后兼容；
  若协议升级（v2），会同步发布新版 automind-pro。
