# 03 — 技能开发 / Skill Development

编写一个 Python 技能并导入 AutoMind。

## 技能结构

一个技能 = 继承 `AbstractSkill` 的类，实现 `async execute(input_data, agent)`，
返回 `SkillResult`。参见本目录 [word_count_skill.py](word_count_skill.py)。

## 本地验证（不启动服务）

```bash
python examples/03-skill-development/word_count_skill.py
# 输出: SkillResult success=True words=5 chars=24 ...
```

## 导入到 Web 工作台

1. 打开工作台 → 侧边栏「🔧 工具面板」→「✨ 技能」分段。
2. 点「📄 导入 .py」选择 `word_count_skill.py`，或
   「📁 加载目录」填写本目录路径。
3. 技能卡片出现后，Agent 即可在任务中调用它。

## 也支持 SKILL.md 技能包

一个文件夹内放 `SKILL.md`（YAML frontmatter: name/description/emoji + 正文指令），
用「📁 加载目录」导入，适合无代码的提示词型技能。
