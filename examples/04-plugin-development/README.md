# 04 — 插件开发 / Plugin Development

编写一个生命周期钩子插件，在任务开始/结束时执行自定义逻辑
（记录日志、发通知、审计上报等）。

## 插件结构

```
~/.automind/plugins/task-timer/
├── plugin.json     # 元信息
└── hooks.py        # get_hooks() -> AgentHooks
```

本目录提供完整示例（[plugin.json](plugin.json) + [hooks.py](hooks.py)）。

## 安装

```bash
# Windows
mkdir "%USERPROFILE%\.automind\plugins\task-timer"
copy examples\04-plugin-development\plugin.json "%USERPROFILE%\.automind\plugins\task-timer\"
copy examples\04-plugin-development\hooks.py "%USERPROFILE%\.automind\plugins\task-timer\"

# Linux/macOS
mkdir -p ~/.automind/plugins/task-timer
cp examples/04-plugin-development/{plugin.json,hooks.py} ~/.automind/plugins/task-timer/
```

## 启用

Web 工作台 → 「🔧 工具面板」→「🧩 插件」→ 找到 `task-timer` → 点「加载」。
之后每次任务执行，插件会记录起止时间与耗时。

## 可用钩子

`before_run / after_parse / before_plan / after_plan / before_tool /
after_tool / after_run / on_error` — 均可选、可同步或异步；
钩子内部异常会被吞掉，**永远不会影响主流程**。
