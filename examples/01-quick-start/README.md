# 01 — 快速开始 / Quick Start

从零启动 AutoMind Web 工作台并执行第一个任务。

## 步骤

```bash
# 1. 安装 —— 二选一：
pip install "automind-agent[web]"   # 从 PyPI（推荐）
pip install -e ".[web]"             # 或在仓库根目录源码安装

# 2. 启动 Web 工作台
python -m automind.server --port 8765

# 3. 浏览器打开
#    http://localhost:8765

# 4. 点击右上角「🔑 API Keys」配置任一提供商的 Key
#    （或提前设置环境变量，如 OPENAI_API_KEY / DEEPSEEK_API_KEY）

# 5. 在输入框输入第一个任务，例如：
#    「在当前目录创建 hello.txt，内容为 Hello AutoMind」
#    模式选「⚙️ 工作」，回车执行。
```

## 预期结果

- 右侧「⚙️ 执行过程」面板实时展示 计划生成 → 逐步执行 → 完成。
- 项目目录出现 `hello.txt`。
- 「📊 统计分析」中任务数 +1、Token 用量更新。

## CLI 方式（可选）

```bash
automind "在当前目录创建 hello.txt，内容为 Hello AutoMind"
automind            # 无参数进入 Rich REPL，/help 查看命令
```
