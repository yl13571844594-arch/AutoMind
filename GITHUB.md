# 上传到 GitHub — 操作说明

仓库已在本地初始化、`.gitignore` / `.gitattributes` / `LICENSE` 已就绪并**确认不含任何密钥**。
下面由**你亲自**创建远程仓库并推送（需要你的账号凭证，我不能代你推送）。

## 已就绪与安全保障（对应「确保以后不会出现警告」）

- **不会泄露密钥**：`.gitignore` 已排除 `.automind_config.json`（含 API Key）、`.automind/`（记忆/检查点/会话）、`dist/`、各类缓存。已扫描待提交文件，无 API Key。
  - ⚠ 之前旧 `.gitignore` 用了**行内注释**（git 不支持），导致密钥一度被暂存——现已修正为独立注释行。
- **不会有 CRLF 警告**：`.gitattributes` 设 `* text=auto eol=lf`，统一行尾；首次提交时的一次性 “CRLF will be replaced by LF” 属正常规范化，之后不再出现。
- **GitHub 能识别许可证**：新增 `LICENSE`（MIT）。
- **不会有 secret-scanning 拦截**：因密钥文件已被忽略，push 不会被 GitHub 推送保护阻断。

## 第 1 步：在 GitHub 网页端建空仓库

登录 https://github.com/ （用户名 `yl13571844594-arch`）→ 右上 **+** → **New repository**：
- Repository name：`automind`（或你喜欢的名字）
- **不要**勾选 “Add README / .gitignore / license”（本地已有）
- 创建后复制仓库地址，如 `https://github.com/yl13571844594-arch/automind.git`

## 第 2 步：本地提交并推送

在项目根目录执行（Git Bash / PowerShell 均可）：

```bash
git add -A
git commit -m "AutoMind v0.5.0"

git branch -M main
git remote add origin https://github.com/yl13571844594-arch/automind.git
git push -u origin main
```

推送时如提示登录：**用户名填 `yl13571844594-arch`，密码处粘贴 Personal Access Token（不是账号密码）**。

### 如何拿 Token（Personal Access Token）

GitHub 已不支持用账号密码 push，需用 PAT：
Settings → Developer settings → **Personal access tokens** → Tokens (classic) → Generate new token →
勾选 `repo` 权限 → 生成后复制（只显示一次），推送时当密码粘贴。

## 后续提交

```bash
git add -A
git commit -m "说明本次改动"
git push
```

## 提醒

- 你的 PyPI 密码此前出现在对话里，请务必已到 pypi.org 改密码 + 开 2FA。
- 若某次不小心把 `.automind_config.json` 提交上去了，请立刻在 PyPI/各模型平台**吊销并轮换对应 API Key**，历史记录里的密钥即使删除也可能被抓取。
