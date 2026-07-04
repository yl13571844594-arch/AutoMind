# 发布到 PyPI — 操作说明（v0.4.0）

分发包已构建并通过校验，可直接上传。**请你亲自执行上传**（原因见下方安全提示）。

## 已就绪

```
dist/automind-0.4.0-py3-none-any.whl   ← twine check: PASSED
dist/automind-0.4.0.tar.gz             ← twine check: PASSED
```

- 包名 `automind` 在 PyPI 上**可用**（未被占用）。
- 已修复一个打包缺陷：`dependencies` 之前被错误地嵌套进 `[project.urls]`，会导致 `pip install` 失败——现已修正。

## ⚠ 安全提示（务必先读）

1. **你的 PyPI 密码已出现在聊天记录里，请立即到 https://pypi.org/manage/account/ 修改密码并开启双因素认证（2FA）。**
2. PyPI 自 2024 年起**不再支持用账号密码上传**，必须用 **API Token**。所以下面用 token 上传，而不是密码。
3. 上传到 PyPI 是**不可撤销**的（同一版本号无法覆盖或删除后重传），请确认无误再执行。

## 第 1 步：创建 API Token

1. 登录 https://pypi.org/ → 头像 → **Account settings** → **API tokens** → **Add API token**。
2. Scope 选 “Entire account”（首次发布新包必须账户级），名称随意，生成后复制以 `pypi-` 开头的 token（只显示一次）。

## 第 2 步：上传

在项目根目录执行（把 `<你的TOKEN>` 换成上一步复制的 token）：

```bash
# 可选：先传到测试仓库演练一次
python -m twine upload --repository testpypi dist/* -u __token__ -p <你的TestPyPI TOKEN>

# 正式发布到 PyPI
python -m twine upload dist/* -u __token__ -p <你的TOKEN>
```

- 用户名固定填 `__token__`（不是你的用户名 yl4530）。
- 或者把 token 写进 `~/.pypirc` 后直接 `python -m twine upload dist/*`。

## 第 3 步：验证

```bash
pip install automind          # 从 PyPI 安装
automind --version            # 应输出 automind 0.4.0
```

## 后续版本

改动后先升 `automind/__init__.py` 的 `__version__`（pyproject 会同步），再：

```bash
rm -rf dist build *.egg-info
python -m build
python -m twine check dist/*
python -m twine upload dist/* -u __token__ -p <你的TOKEN>
```
