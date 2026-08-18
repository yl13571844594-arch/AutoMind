## 这个 PR 做了什么 / What does this PR do?

<!-- 一两句话说清楚。如果修的是 bug，请说明**现象**而不只是"修复 xxx"。 -->

## 关联 Issue / Related issue

<!-- 例：Closes #123。没有关联 Issue 也可以，但请在上面把背景讲清楚。 -->

## 改动类型 / Type

- [ ] 🐛 Bug 修复
- [ ] ✨ 新功能
- [ ] ♻️ 重构（不改变外部行为）
- [ ] 📝 文档
- [ ] 🧪 测试
- [ ] 🔧 构建 / CI

## 自查 / Checklist

- [ ] `pytest -q` 全部通过
- [ ] `ruff check .` 无告警
- [ ] 涉及前端时 `cd web && npm run build` 通过
- [ ] **修 bug 时**：先补了一条能复现该 bug 的失败用例，再让它变绿
- [ ] 新增/改动的行为有对应测试
- [ ] 面向用户的改动已更新 `CHANGELOG.md`
- [ ] 改动涉及版本号时，`automind/__init__.py` / `pyproject.toml` /
      `web/package.json` / `desktop/installer.iss` 已同步
      （`pytest tests/test_version_consistency.py` 会校验）

## 验证方式 / How was this verified?

<!-- 你实际怎么确认它好使的？跑了什么命令、看到什么输出、点了哪个界面。
     "应该没问题"不算验证。 -->
