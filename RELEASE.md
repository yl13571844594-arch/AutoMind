# 发布说明 — v0.6.0（社区版 / 商业版分离）

## v0.6.0 变更摘要

**版本体系（开源核心模式）**

- 新增 `automind/core/edition.py`：社区/专业/企业三版本的特性门控与
  **稳定扩展协议 v1**；社区核心零商业代码，商业能力由独立的
  `automind-pro` 包（`pro/` 目录，闭源）运行时注入。
- 迁入商业包的功能：🤝 协同模式（多智能体）、🔁 循环模式（Loop
  Engineering）、⏰ 定时任务、📊 高级统计仪表盘（专业版）；
  👥 会话级 Agent 池 / 执行态多用户隔离（企业版）。
- 社区版访问商业端点返回 `403 + 升级提示`；Web 界面显示版本徽标，
  商业入口显示 🔒；`/api/health`、`/api/status` 返回 `edition` 与 `features`。
- 离线许可证（`AUTOMIND_LICENSE` / `.automind_license`），HMAC 签名校验，
  无效或过期自动降级社区版，服务照常启动。
- 安全能力（鉴权/限流/密钥脱敏/审计）**保留在社区版**。
- 详见 [docs/EDITIONS.md](docs/EDITIONS.md)。

**其他**

- 共享角色提示词迁至 `automind/core/prompts.py`（社区/商业共用的稳定资源）。
- 打包修正：社区包显式 `include = ["automind", "automind.*"]`，
  杜绝商业代码混入开源产物。

## 构建社区版发布物

```bash
python scripts/build_community.py
```

产物（`dist/`）：

```
automind_agent-0.6.0-py3-none-any.whl      ← pip 安装包
automind_agent-0.6.0.tar.gz                ← sdist
automind-community-0.6.0-src.zip           ← 开源上传源码包（白名单收集）
```

脚本会自动**审计**产物：任何产物中出现 `automind_pro`、许可证、
`.automind_config.json` 等敏感/商业内容即构建失败。

## 构建商业版（内部）

```bash
cd pro && python -m build          # automind_pro-0.6.0 wheel（商业渠道分发）
python -m automind_pro.license PRO 20271231 acme   # 生成客户许可证
```

## 上传 PyPI（社区版）

PyPI 不支持账号密码上传，需 API Token（https://pypi.org/manage/account/ →
API tokens；用户名固定填 `__token__`）：

```bash
python -m twine check dist/automind_agent-0.6.0*
# 可选：先传 TestPyPI 演练
python -m twine upload --repository testpypi dist/automind_agent-0.6.0* -u __token__ -p <TOKEN>
# 正式发布（不可撤销，同版本号无法覆盖）
python -m twine upload dist/automind_agent-0.6.0* -u __token__ -p <TOKEN>
```

> ⚠ 切勿上传 `automind_pro` 相关文件到 PyPI；商业包 classifiers 已含
> `Private :: Do Not Upload`（PyPI 会拒收），但请勿依赖这一层兜底。

## 验证

```bash
pip install automind-agent==0.6.0
python -c "import automind; print(automind.__version__)"   # 0.6.0
python -m automind.server --port 8765                       # /api/health → edition: community
```

## 后续版本

1. 升 `automind/__init__.py` 的 `__version__`（唯一数据源，pyproject 同步改）；
2. 商业包同步升 `pro/automind_pro/__init__.py` 与 `pro/pyproject.toml`；
3. `python scripts/build_community.py` → twine check → 上传。
