# AutoMind 已确认 Bug 追踪清单

> **生成日期**: 2026-06-30 | **来源**: 第二轮源码深度审查（30+ 文件）
> **用途**: 供下次修复会话逐条修复 | **估算工时**: 4-6 天（单人）

---

## 📊 统计概览

| 严重度 | 数量 | 占比 |
|:-----:|:---:|:----:|
| 🔴 **严重 Bug** | 5 | 24% |
| 🟡 **重要 Bug** | 11 | 52% |
| 🟢 **常规 Bug** | 5 | 24% |
| **合计** | **21** | 100% |

**修复优先级建议**: 按 P0 → P1 → P2 顺序，每个 Bug 修复后运行 `pytest tests/` 确保不回归。

---

# P0 — 紧急修复（5 个，1-2 天）

> 这些 Bug 会导致**功能完全失效**或**安全漏洞**，修复前不宜用于生产。

---

## B-01: `plan_executor.py` — 自我纠错结果永不执行

| 字段 | 内容 |
|------|------|
| **文件** | `automind/planning/plan_executor.py` |
| **行号** | 128-145 |
| **发现方式** | 静态代码分析 + 逻辑追踪 |
| **首次发现** | 2026-06-30 第二轮审查 |

### 问题描述

当 `execute_goal` 失败后，`_handle_failure` 会调用 LLM 生成一个"修正后的动作"，将这个修正赋值给 `goal.assigned_action`，并将 goal 状态设为 `CORRECTED`。但是——**修正后的动作永远不会被重新执行**。主执行循环（`execute`）已经跳过了这个 goal，`_get_next_goal` 返回的是下一个 PENDING 目标。结果就是：AutoMind 在界面上显示"已修正"，但代码**根本没变**。

```python
# plan_executor.py ~128-145 — 当前逻辑（简化）
async def execute(self, plan, ...):
    while True:
        goal = self._get_next_goal(plan)  # ← 只返回 PENDING 目标
        if not goal:
            break
        result = await self._execute_goal(goal, ...)
        if not result.success:
            await self._handle_failure(goal, ...)  # ← 这里改了动作
            # goal 被设为 CORRECTED，循环继续取下一个
            # 修正后的动作 永 远 不 会 执 行
```

### 影响范围

- 所有使用 `PLAN_AND_EXECUTE` 模式的场景
- 影响面：**编程模式**和**工作模式**中 AutoMind 声称"已修复"但实际没改

### 修复方案

```python
async def execute(self, plan, ...):
    while True:
        goal = self._get_next_goal(plan)
        if not goal:
            break
        result = await self._execute_goal(goal, ...)
        if not result.success:
            await self._handle_failure(goal, ...)
            # 修复：将修正后的 goal 重置为 PENDING，
            # 让它在本轮或下一轮被重新执行
            goal.status = GoalStatus.PENDING
            continue  # ← 立即重试修正后的动作
        else:
            goal.status = GoalStatus.COMPLETED
```

### 验证方法

```python
# 测试：修正后确认动作被重新执行
plan = await planner.plan("创建并写入一个文件")
goal = plan.root_goal.children[0]
goal.assigned_action.tool_name = "non_existent_tool"  # 模拟失败
result = await executor.execute(plan)
# 断言: result 包含修正后动作的执行记录，而非跳过
assert any(s.tool_name != "non_existent_tool" for s in result.steps)
```

---

## B-02: `dependency_graph.py` — 资源依赖边方向反转

| 字段 | 内容 |
|------|------|
| **文件** | `automind/planning/dependency_graph.py` |
| **行号** | 240-246 |
| **发现方式** | 代码审查 + 逻辑推理 |

### 问题描述

在 `add_resource_dependency` 方法中，当 g1 的效果创建了 g2 需要的资源时，依赖边的方向被错误地设置为 `g1 → g2`（即 g1 依赖 g2）。但语义应该是"g1 必须先执行，因为 g2 依赖 g1 的输出"，所以正确的边方向是 `g2 → g1`（g2 依赖 g1）。

```python
# dependency_graph.py:240-246 — 当前（方向反了）
def add_resource_dependency(self, g1: Goal, g2: Goal, resource: str) -> None:
    """如果 g1 的 expected_effects 包含 resource，而 g2 的 preconditions 需要它..."""
    self.graph.add_edge(g1.id, g2.id)  # ← 应该改成 g2.id, g1.id
```

### 影响

| 场景 | 当前表现 | 修复后 |
|------|---------|--------|
| A 创建文件，B 读取文件 | B 可能在 A 之前执行 → **文件不存在崩溃** | A 先执行创建，B 再读取 ✅ |
| A 安装依赖，B 导入模块 | B 可能在 A 之前执行 → **ModuleNotFoundError** | A 先 pip install，B 再 import ✅ |
| 多目标并行组检测 | 依赖图混乱 → 无法正确分组 | 正确的执行顺序分组 ✅ |

### 修复方案

```python
def add_resource_dependency(self, g1: Goal, g2: Goal, resource: str) -> None:
    """如果 g1 的 expected_effects 包含 resource，而 g2 的 preconditions 需要它，
    则 g2 应在 g1 之后执行。"""
    # g2 依赖 g1 → 边方向从 g2 指向 g1
    self.graph.add_edge(g2.id, g1.id)
```

### 验证方法

```python
# 测试：验证依赖方向
g1 = Goal(id="create_file", description="创建文件", 
          expected_effects=[Predicate(name="file_exists", args=["a.txt"])])
g2 = Goal(id="read_file", description="读取文件",
          preconditions=[Predicate(name="file_exists", args=["a.txt"])])
dg = TaskDependencyGraph()
dg.add_resource_dependency(g1, g2, "file_exists(a.txt)")
order = dg.get_execution_order()
# g1 必须在 g2 之前
assert order.index("create_file") < order.index("read_file")
```

---

## B-03: `react_executor.py` — 工具调用异常未捕获

| 字段 | 内容 |
|------|------|
| **文件** | `automind/planning/react_executor.py` |
| **行号** | ~118 |
| **发现方式** | 代码审查 |

### 问题描述

ReAct 循环中调用 `self.tool_registry.dispatch(name, **args)` 时，如果工具本身抛出异常（如 `pip install` 失败了、网络超时等），**整个 ReAct 循环会崩溃**，当前会话的所有上下文丢失，用户必须重新开始。

```python
# react_executor.py ~118 — 当前
async def _execute_tool_call(self, tool_call: ToolCall) -> str:
    result = await self.tool_registry.dispatch(  # ← 未加 try/except
        tool_call.name, **tool_call.arguments
    )
    return json.dumps(result.to_dict(), ensure_ascii=False)
```

### 影响范围

- **所有使用 ReAct 循环的场景**（编程模式 💻、循环模式 🔁、对话模式中的工具调用）
- 一个小错误（如路径不存在）→ 整个任务归零

### 修复方案

```python
async def _execute_tool_call(self, tool_call: ToolCall) -> str:
    try:
        result = await self.tool_registry.dispatch(
            tool_call.name, **tool_call.arguments
        )
        return json.dumps(result.to_dict(), ensure_ascii=False)
    except Exception as e:
        # 将异常包装为工具执行失败的结果，让 LLM 继续决策
        error_result = ToolResult(
            tool_name=tool_call.name,
            success=False,
            error=f"Tool execution failed: {type(e).__name__}: {e}",
        )
        return json.dumps(error_result.to_dict(), ensure_ascii=False)
```

### 验证方法

```python
# 测试：异常不崩溃循环
executor = ReActExecutor(llm=mock_llm, tool_registry=registry)
tool_call = ToolCall(id="1", name="non_existent_tool", args={})
result = await executor._execute_tool_call(tool_call)
# 不抛异常，返回错误结果
assert "failed" in result.lower()
```

---

## B-04: `checkpoint.py:40` — `json.dump(default=str)` 静默损坏数据

| 字段 | 内容 |
|------|------|
| **文件** | `automind/state/checkpoint.py` |
| **行号** | 40 |
| **发现方式** | 代码审查（Anti-pattern 检测） |

### 问题描述

```python
json.dump(data, f, indent=2, ensure_ascii=False, default=str)
```

`default=str` 会将任何不可 JSON 序列化的对象（如 `datetime`、`Path`、自定义 Pydantic 类型等）**悄无声息地转为字符串**。在 `save` 时没有任何警告，在 `load` 时这些字段无法恢复为原始类型，导致：
- `datetime` → `"2026-06-30 12:00:00"`（字符串，不再是 datetime 对象）
- `Path("/foo")` → `"/foo"`（字符串，不再是 Path 对象）
- 自定义类型 → 不可逆的字符串表示

当 Agent 恢复后访问这些字段时，类型不匹配导致意外行为。

### 影响范围

- 所有使用检查点保存/恢复的场景
- **数据损坏不可逆**——一旦保存，坏数据就会覆盖好数据

### 修复方案

```python
# 方案 A（推荐）：移除 default=str，让不可序列化类型立即抛错
json.dump(data, f, indent=2, ensure_ascii=False)
# 这会让开发阶段就发现序列化问题，而不是在生产中静默损坏

# 方案 B（如果需要兜底）：自定义序列化器
class _CheckpointEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Path):
            return str(obj)
        if hasattr(obj, "model_dump"):  # Pydantic v2
            return obj.model_dump(mode="json")
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

json.dump(data, f, indent=2, ensure_ascii=False, cls=_CheckpointEncoder)
```

### 验证方法

```python
# 测试：保存带 datetime 的状态，恢复后类型正确
state = AgentState(session_id="test", last_updated=datetime.now(timezone.utc))
mgr = CheckpointManager(tmp_path)
cid = await mgr.save(state)
restored = await mgr.load(cid)
assert isinstance(restored.last_updated, datetime)  # 不是字符串！
```

---

## B-05: `terminal.py:77-78` — Shell 注入漏洞

| 字段 | 内容 |
|------|------|
| **文件** | `automind/tools/terminal.py` |
| **行号** | 77-78 |
| **发现方式** | 安全审查 |

### 问题描述

```python
process = await asyncio.create_subprocess_shell(
    cmd,  # ← 直接拼接用户输入到 shell
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)
```

使用 `create_subprocess_shell` + 用户输入的 `cmd` 直接传给 shell 解析。如果用户输入中包含 `; rm -rf /`、`$(malicious)`、`` `malicious` `` 等，命令会逃逸。例如：

```
> ls; echo "你被黑了"
# → 先执行 ls，再执行 echo "你被黑了"
```

`shell=True` 模式是 Python 安全指南中明确警告不要用于用户输入的。

### 影响范围

- **所有通过终端工具执行的命令**
- 编程模式中 `pip install some-package`（如果包名含特殊字符）
- 办公自动化中操作文件路径（如果路径含空格）

### 修复方案

```python
# 用 create_subprocess_exec 替代 create_subprocess_shell
import shlex

# 方案 A：使用 shlex.split 分解命令
parts = shlex.split(cmd)
process = await asyncio.create_subprocess_exec(
    *parts,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
)

# 方案 B：严格禁止 shell 元字符（降级方案）
if not shlex.split(cmd) or cmd != shlex.join(shlex.split(cmd)):
    raise PermissionError("命令包含 shell 元字符，已拒绝执行")
```

### 验证方法

```python
# 测试：shell 注入被拦截
tool = TerminalTool()
result = await tool.execute(command="echo hello; rm -rf /")
assert not result.success  # 不应成功
# 安全命令正常
result = await tool.execute(command="echo hello")
assert result.success
```

---

# P1 — 重要修复（11 个，2-3 天）

> 这些 Bug 不影响程序运行，但导致**功能不正确**或**数据不可信**。

---

## B-06: `retry_handler.py:101` — HALF_OPEN 节流器永不触发

| 字段 | 内容 |
|------|------|
| **文件** | `automind/reflection/retry_handler.py` |
| **行号** | 101 |
| **严重度** | 🟡 重要 |
| **修复难度** | 🟢 极低（加一行） |

### 问题描述

```python
if self._circuit_state == CircuitState.HALF_OPEN:
    if self._half_open_count >= self.circuit_config.half_open_max_requests:
        raise RuntimeError("Circuit breaker HALF_OPEN — too many test requests")
```

`_half_open_count` **从未被递增**（初始化 0，进入 HALF_OPEN 时重置 0，成功后重置 0）。检查 `0 >= 1` 永远为 False，所以 HALF_OPEN 状态下无任何保护，所有请求直通，熔断器形同虚设。

### 修复方案

在通过请求的位置加一行：

```python
if self._circuit_state == CircuitState.HALF_OPEN:
    if self._half_open_count >= self.circuit_config.half_open_max_requests:
        raise RuntimeError("Circuit breaker HALF_OPEN — too many test requests")
    self._half_open_count += 1  # ← 加这一行
```

---

## B-07: `self_correction.py:94` — 重试时错误信息丢失

| 字段 | 内容 |
|------|------|
| **文件** | `automind/reflection/self_correction.py` |
| **行号** | 94 |
| **严重度** | 🟡 重要 |
| **修复难度** | 🟢 极低 |

### 问题描述

```python
record = CorrectionRecord(
    ...
    error=error_message if iteration == 0 else "",
    ...
)
```

第 2 次及以后的重试中，`error` 字段被设为空字符串。当你在调试一个经历了 3 次修正才解决的问题时，你只能看到第 1 次的错误，第 2、3 次的错误信息全丢了。

### 修复方案

```python
error=error_message,  # 每次都保存当前错误
```

---

## B-08: `long_term.py:70` — `[{}] * n` 共享同一个 dict

| 字段 | 内容 |
|------|------|
| **文件** | `automind/memory/long_term.py` |
| **行号** | 70 |
| **严重度** | 🟡 重要 |
| **修复难度** | 🟢 极低 |

### 问题描述

```python
metadatas = [{}] * len(documents)  # ← 所有元素指向同一个 dict 对象
```

Python 中 `[{}] * n` 创建的是**对同一个 dict 的 n 个引用**。当后续代码修改其中一个 metadata（如 `metadatas[0]["key"] = "val"`），所有元素都被修改。这在批量添加文档时会导致 metadata 污染。

### 修复方案

```python
metadatas = [{} for _ in documents]  # 每个元素是独立的 dict
```

---

## B-09: `knowledge_graph.py:77,86` — 关系类型存入但读回硬编码

| 字段 | 内容 |
|------|------|
| **文件** | `automind/memory/knowledge_graph.py` |
| **行号** | 77, 86 |
| **严重度** | 🟡 重要 |
| **修复难度** | 🟢 低 |

### 问题描述

`add_relation` 将关系类型（如"依赖"、"调用"、"包含"）存入源节点的属性字典中（键为 `rel_{source}_{target}`），但 `get_relations` 读回时**总是返回 `"type": "related"`**——它从未查询存储的元数据。

```python
def get_relations(self, entity_id: str) -> list[dict]:
    ...
    for target in targets:
        result.append({
            "source": entity_id,
            "target": target,
            "type": "related",  # ← 永远返回这个硬编码值
        })
```

### 修复方案

```python
def get_relations(self, entity_id: str) -> list[dict]:
    ...
    for target in targets:
        edge_key = f"rel_{entity_id}_{target}"
        stored_type = "related"
        node_data = self.graph._nodes.get(entity_id, {})
        if edge_key in node_data:
            stored_type = node_data[edge_key].get("type", "related")
        result.append({
            "source": entity_id,
            "target": target,
            "type": stored_type,  # ← 从存储中读取真实类型
        })
```

---

## B-10: `reasoning.py:178-187` — ToT 路径追溯断裂

| 字段 | 内容 |
|------|------|
| **文件** | `automind/planning/reasoning.py` |
| **行号** | 178-187 |
| **严重度** | 🟡 重要 |
| **修复难度** | 🟡 中 |

### 问题描述

`_get_path` 方法本应回溯从根节点到当前叶节点的完整路径，但父指针查找缺失。在 Tree-of-Thought 搜索中，`search` 返回的只是一个叶节点，缺少它如何到达那里的推理链。

```python
def _get_path(self, node_id: str) -> list[str]:
    # 当前：只返回叶节点本身
    return [node_id]
    
    # 应该：沿着 parent_id 链接回溯到根
    path = []
    current = node_id
    while current:
        path.append(current)
        current = self._parents.get(current)  # 需要 _parents 映射
    return list(reversed(path))
```

### 修复方案

需要在搜索树中维护父指针映射：

```python
# reasoning.py — search 方法中
def search(self, root: ThoughtNode) -> ThoughtNode | None:
    self._parents: dict[str, str] = {}  # child_id → parent_id
    ...
    for child in self._expand(node):
        self._parents[child.id] = node.id
        ...
```

---

## B-11: `resource_manager.py:59-62` — TokenBucket 竞争条件

| 字段 | 内容 |
|------|------|
| **文件** | `automind/state/resource_manager.py` |
| **行号** | 59-62 |
| **严重度** | 🟡 重要 |
| **修复难度** | 🟡 中 |

### 问题描述

`TokenBucketRateLimiter.acquire` 在令牌不足时 `await asyncio.sleep(wait_time)`，期间其他协程可以调用 `acquire` 从快速路径消耗令牌，导致 `self._tokens` 变为负数——超额放行。

```python
async def acquire(self, tokens: int = 1) -> bool:
    if self._tokens >= tokens:
        self._tokens -= tokens  # 快速路径
        return True
    wait_time = (tokens - self._tokens) / self._rate
    await asyncio.sleep(wait_time)  # ← 其他协程在此期间可以消耗令牌
    self._refill()
    self._tokens -= tokens  # ← 可能变为负数
```

### 修复方案

使用 `asyncio.Lock` 保护关键区：

```python
async def acquire(self, tokens: int = 1) -> bool:
    async with self._lock:  # ← 加锁
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        wait_time = (tokens - self._tokens) / self._rate
    # 锁外等待（不阻塞其他协程检查）
    await asyncio.sleep(wait_time)
    async with self._lock:  # ← 重新加锁后操作
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False
```

---

## B-12: `input_parser.py:60-62` — 图片读取 TOCTOU 竞争

| 字段 | 内容 |
|------|------|
| **文件** | `automind/context/input_parser.py` |
| **行号** | 60-62 |
| **严重度** | 🟡 重要 |
| **修复难度** | 🟢 低 |

### 问题描述

```python
if img_path.exists():           # 检查存在
    msg.images.append(img_path.read_bytes())  # 读取文件
```

在 `exists()` 和 `read_bytes()` 之间，文件可能被删除（TOCTOU 竞争）。同时，没有文件大小限制——一个 2GB 的图片引用会直接 OOM。

### 修复方案

```python
MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20MB

try:
    if img_path.stat().st_size > MAX_IMAGE_BYTES:
        logger.warning(f"图片过大，已跳过: {img_path}")
        continue
    data = img_path.read_bytes()
    msg.images.append(data)
except (FileNotFoundError, PermissionError) as e:
    logger.warning(f"读取图片失败: {e}")
    continue
```

---

## B-13: `project_indexer.py:210` — 符号链接导致索引崩溃

| 字段 | 内容 |
|------|------|
| **文件** | `automind/context/project_indexer.py` |
| **行号** | 210 |
| **严重度** | 🟡 重要 |
| **修复难度** | 🟢 低 |

### 问题描述

```python
rel = str(path.relative_to(self.project_root))
```

如果 `path` 是一个指向项目目录外的符号链接，`relative_to()` 会抛出 `ValueError: path is not relative to ...`，导致整个索引构建崩溃。

### 修复方案

```python
try:
    rel = str(path.relative_to(self.project_root))
except ValueError:
    continue  # 跳过指向项目外的符号链接
```

---

## B-14: `project_indexer.py:227,245` — 缓存文件未指定 UTF-8 编码

| 字段 | 内容 |
|------|------|
| **文件** | `automind/context/project_indexer.py` |
| **行号** | 227, 245 |
| **严重度** | 🟡 重要 |
| **修复难度** | 🟢 极低 |

### 问题描述

```python
with open(cache_path) as f:       # 行 227 — 默认系统编码
with open(cache_path, "w") as f:  # 行 245 — 默认系统编码
```

在 Windows 上默认编码为 `cp1252`，包含非 ASCII 字符（中文路径、Unicode 文件名）的缓存数据会被损坏。

### 修复方案

```python
with open(cache_path, encoding="utf-8") as f:
with open(cache_path, "w", encoding="utf-8") as f:
```

---

## B-15: `terminal.py:84-86` — 僵尸进程残留

| 字段 | 内容 |
|------|------|
| **文件** | `automind/tools/terminal.py` |
| **行号** | 84-86 |
| **严重度** | 🟡 重要 |
| **修复难度** | 🟡 中 |

### 问题描述

`asyncio.wait_for` 超时后，`asyncio.TimeoutError` 被抛出，但子进程并未被杀死。它继续在后台运行，变成僵尸进程。

```python
try:
    stdout, stderr = await asyncio.wait_for(
        process.communicate(), timeout=timeout
    )
except asyncio.TimeoutError:
    # ← process 还在后台运行！
    return ToolResult(success=False, error=f"Command timed out after {timeout}s")
```

### 修复方案

```python
except asyncio.TimeoutError:
    try:
        process.kill()  # 杀进程
        await process.wait()  # 等待进程彻底结束，避免僵尸
    except ProcessLookupError:
        pass  # 进程可能已经自己结束了
    return ToolResult(success=False, error=f"Command timed out after {timeout}s")
```

---

## B-16: `permissions.py:159-166` — `allowed_paths` 白名单未实现

| 字段 | 内容 |
|------|------|
| **文件** | `automind/tools/permissions.py` |
| **行号** | 159-166 |
| **严重度** | 🟡 重要 |
| **修复难度** | 🟡 中 |

### 问题描述

`PermissionEngine` 中定义了 `allowed_paths` 和 `denied_paths` 两个配置字段。文档说 `check_path` 方法"检查文件路径是否在允许范围内"。但代码**只检查了 `denied_paths`**，完全忽略了 `allowed_paths`：

```python
def check_path(self, path: str) -> bool:
    # 只检查了拒绝列表
    for denied in self.config.denied_paths:
        if Path(path).resolve() == Path(denied).resolve():
            return False
    return True  # ← 任何路径都被允许，只要不在拒绝列表中
```

### 修复方案

```python
def check_path(self, path: str) -> bool:
    resolved = Path(path).resolve()
    
    # 如果有白名单，路径必须在白名单内
    if self.config.allowed_paths:
        allowed = False
        for allowed_path in self.config.allowed_paths:
            allowed_resolved = Path(allowed_path).resolve()
            if resolved == allowed_resolved or allowed_resolved in resolved.parents:
                allowed = True
                break
        if not allowed:
            return False
    
    # 检查拒绝列表
    for denied in self.config.denied_paths:
        if resolved == Path(denied).resolve():
            return False
    
    return True
```

---

## B-17: `code_analyzer.py:232-237` — 类型注解检测完全失效

| 字段 | 内容 |
|------|------|
| **文件** | `automind/context/code_analyzer.py` |
| **行号** | 232-237 |
| **严重度** | 🟡 重要 |
| **修复难度** | 🟡 中 |

### 问题描述

```python
annotated = 0
for s in analysis.symbols:
    if s.kind in ("function", "method"):
        annotated += 1  # 简化处理——只要函数存在就加 1
if annotated > 0:
    profile.type_annotations_used = True
```

这段代码**只要项目中有一个函数**就把 `type_annotations_used` 设为 `True`，不管那个函数有没有类型注解。注释说"简化处理"，但这是功能性地错误——风格分析完全失去了意义。

### 修复方案

需要实际检查函数的 AST 节点是否有类型注解：

```python
import ast

annotated = 0
total_functions = 0
for s in analysis.symbols:
    if s.kind in ("function", "method"):
        total_functions += 1
        try:
            node = ast.parse(Path(s.file_path).read_text())
            # 在 AST 中查找该函数并检查参数/返回值的 annotation
            func_nodes = [n for n in ast.walk(node) 
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and n.name == s.name]
            for func in func_nodes:
                has_annotations = (
                    func.returns is not None  # 返回值注解
                    or any(p.annotation for p in func.args.args)  # 参数注解
                )
                if has_annotations:
                    annotated += 1
                    break
        except Exception:
            continue

if total_functions > 0:
    profile.type_annotations_used = (annotated / total_functions) > 0.5
```

---

# P2 — 常规修复（5 个，1 天）

> 这些 Bug 不影响核心功能，修复后提升体验或防止未来问题。

---

## B-18: `consistency_checker.py:142` — 字符串比较代替枚举

| 字段 | 内容 |
|------|------|
| **文件** | `automind/reflection/consistency_checker.py` |
| **行号** | 142 |
| **严重度** | 🟢 常规 |
| **修复难度** | 🟢 极低 |

### 问题描述

```python
completed = [g for g in all_goals if g.status.value == "completed"]
failed = [g for g in all_goals if g.status.value == "failed"]
```

使用字符串 `"completed"` 而非 `GoalStatus.COMPLETED` 枚举值。如果枚举被重构（如 `COMPLETED = "done"`），这段代码静默失效。

### 修复方案

```python
from automind.core.types import GoalStatus

completed = [g for g in all_goals if g.status == GoalStatus.COMPLETED]
failed = [g for g in all_goals if g.status == GoalStatus.FAILED]
```

---

## B-19: `consistency_checker.py:165-171` — Datalog 规则定义但从不查询

| 字段 | 内容 |
|------|------|
| **文件** | `automind/reflection/consistency_checker.py` |
| **行号** | 165-171 |
| **严重度** | 🟢 常规 |
| **修复难度** | 🟢 低 |

### 问题描述

`_setup_rules` 方法向 Datalog 引擎添加了 `conflict` 规则，但 `check_resource_conflicts` 方法（行 117）使用自己的命令式循环来检测资源冲突，**从未调用** `self.engine.query(...)`。这是一个死代码。

### 修复方案

方案 A：删除 `_setup_rules` 方法和 `DatalogEngine` 依赖。
方案 B：重构 `check_resource_conflicts` 使用 Datalog 查询：

```python
def check_resource_conflicts(self, plan: HierarchicalPlan) -> list[str]:
    # 先用 Datalog 规则查询
    conflicts = self.engine.query("conflict", ["X", "Y"])
    # ... 处理冲突
```

---

## B-20: `entity_memory.py:119` — 实体 ID 静默覆盖

| 字段 | 内容 |
|------|------|
| **文件** | `automind/memory/entity_memory.py` |
| **行号** | 119 |
| **严重度** | 🟢 常规 |
| **修复难度** | 🟢 低 |

### 问题描述

如果 `_llm_extract` 或 `_simple_extract` 产生两个相同 ID 的实体，第二个静默覆盖第一个：

```python
self._entities[e.id] = e  # ← 同 ID 直接覆盖
```

### 修复方案

```python
if e.id in self._entities:
    existing = self._entities[e.id]
    # 合并属性
    existing.properties.update(e.properties)
    logger.debug(f"合并实体 '{e.name}' (ID: {e.id})")
else:
    self._entities[e.id] = e
```

---

## B-21: `context_manager.py:50,121` — Token 计数多次压缩后漂移

| 字段 | 内容 |
|------|------|
| **文件** | `automind/context/context_manager.py` |
| **行号** | 50, 121-123 |
| **严重度** | 🟢 常规 |
| **修复难度** | 🟡 中 |

### 问题描述

`_estimated_tokens` 只在添加非 system 消息时累加，但压缩时减去的是近似值（基于 `_SimpleTokenizer` 计数）。多次压缩后，这个计数会逐渐偏离实际值，导致过早或过晚触发压缩。

### 修复方案

每次压缩后重建计数：

```python
def _recalculate_tokens(self) -> None:
    """从现有消息重新计算 token 总数。"""
    self._estimated_tokens = sum(
        self._token_counter.count(m.content) 
        for m in self._messages
    )
```

在 `compress` 方法的最后调用此函数。

---

# 附：修复顺序建议

## 推荐的修复批次

```
第一批（第 1 天）：P0 × 5
 ├── B-05 terminal shell 注入（安全漏洞，最紧急）
 ├── B-01 plan_executor 纠错死代码（功能完全失效）
 ├── B-04 checkpoint default=str（数据损坏）
 ├── B-02 dependency_graph 方向反（调度错乱）
 └── B-03 react_executor 未捕获异常（循环崩溃）

第二批（第 2-3 天）：P1 × 11
 ├── B-09 knowledge_graph 关系类型（记忆失效）
 ├── B-10 reasoning 路径追溯（推理错误）
 ├── B-11 resource_manager 竞态（高并发问题）
 ├── B-07 self_correction 错误丢失（调试无用）
 ├── B-08 long_term 共享 dict（数据污染）
 ├── B-06 retry_handler 节流器（熔断失效）
 ├── B-12 input_parser TOCTOU（间歇崩溃）
 ├── B-13 project_indexer 符号链接（索引崩溃）
 ├── B-14 缓存编码（Win 下中文损坏）
 ├── B-15 terminal 僵尸进程（进程泄漏）
 └── B-16 permissions 白名单（安全门控缺口）

第三批（第 4 天）：P2 × 5
 ├── B-17 code_analyzer 类型检测（假报告）
 ├── B-18 consistency_checker 枚举（未来重构隐患）
 ├── B-19 死 Datalog 规则
 ├── B-20 entity_memory 覆盖
 └── B-21 context_manager token 漂移
```

## 每批后的验证

```bash
# 每批修复后运行
pytest tests/ -v

# 全部修复后运行全套
pytest tests/ -v --tb=short
python -c "import automind; print('OK')"
```

---

> **注意**: 此清单仅包含**代码审查中发现的逻辑 Bug**，不含：
> - 已在 `AUTOMIND_REFACTOR_PLAN.md` §2-§14 中记录但未实现的功能改进（如 server.py 拆分、检查点恢复、真实 Embedding 等）
> - §14 中已写入计划但未编码实现的新功能（CI/CD、TUI、PyPI 发布等）
> - 已在第六轮修复的 3 个问题（沙箱缺陷、路径穿越、版本号统一）
>
> 修复此清单的 21 个 Bug 预计需要 **4-6 天（单人）**，修复后 AutoMind 综合稳定性可从当前 ~52 分提升至 ~82 分。


请继续优化修复并更新版本号，请将多 Agent 协同融入工作模式（默认开启可手动选择开启或关闭），Loop 循环融入工作/编程模式（默认开启可手动选择开启或关闭），增强测试验证，请给编程模式增强代码生成器技能，编程模式每轮代码修改后自动验证，形成真正的 TDD 闭环，无论编程模式还是工作模式要求实现 "自主完成任务闭环"——多 Agent 审查 + Loop 验证 + TDD 测试，支持并行执行（异步 asyncio.gather），支持子任务缓存，支持MCP 工具共享，同时更新功能手册。

