"""v1.11.11 前端：待办 / 提问弹窗 / 交付卡片 / 子智能体面板 / /compact 回归。

前端没有 JS 测试栈，故从**源码与构建产物**两侧做结构性断言（沿用 v1.4 起的做法）。
这一版盯住的是五条链路，共同点都是"界面不报错、但不显示"：

    1. WS 事件字段与 React 处理函数必须一一对应 —— 字段名错一个（比如把
       ``output_tail`` 写成 ``output``），子智能体面板只会安静地空着；
    2. 提问弹窗是**阻塞式**的：回答必须真的发出去（user_answer），超时不能当成同意；
    3. 交付卡片只能存路径、点开现读 —— 落盘的消息里塞文件内容会把 localStorage 撑爆，
       且刷新后看到的仍是旧内容；
    4. 子智能体是专业版功能，只有 ``status.features.multi_agent`` 为真时才显示；
    5. 经典兜底界面（/legacy）与 React 同步，否则构建失败时功能直接消失。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "src"
STATIC = ROOT / "automind" / "static"
DIST = STATIC / "dist"


def _web(rel: str) -> str:
    return (WEB / rel).read_text(encoding="utf-8")


def _static(rel: str) -> str:
    return (STATIC / rel).read_text(encoding="utf-8")


class TestPanelStore:
    """store/panel.ts：待办 / 提问 / 子智能体 / 压缩中 四块新状态。"""

    @staticmethod
    def _src() -> str:
        return _web("store/panel.ts")

    def test_todo_state(self):
        s = self._src()
        assert "export interface TodoItem" in s
        assert "export interface TodoCounts" in s
        for field in ("status: 'pending' | 'in_progress' | 'completed' | string",
                      "todos: TodoItem[]", "todoCounts: TodoCounts | null"):
            assert field in s, f"缺待办字段：{field}"
        assert "setTodos: (todos: TodoItem[], counts?: Partial<TodoCounts>) => void" in s
        assert "clearTodos: () => void" in s

    def test_question_state(self):
        s = self._src()
        assert "export interface UserQuestion" in s
        assert "export interface PendingQuestion" in s
        # 倒计时需要本地起点：askedAt 不能省
        assert "askedAt: number" in s
        assert "question: PendingQuestion | null" in s
        assert "setQuestion: (q: Omit<PendingQuestion, 'askedAt'> & { askedAt?: number }) => void" in s
        assert "clearQuestion: () => void" in s

    def test_subagent_state(self):
        s = self._src()
        assert "export interface SubAgentInfo" in s
        for field in ("output_tail?: string", "steps?: number", "tokens?: number"):
            assert field in s, f"SubAgentInfo 缺字段：{field}"
        assert "subagents: SubAgentInfo[]" in s
        assert "pushSubagent: (a: SubAgentInfo) => void" in s
        assert "updateSubagent: (agentId: string, patch: Partial<SubAgentInfo>) => void" in s
        assert "clearSubagents: () => void" in s

    def test_compact_busy(self):
        s = self._src()
        assert "compactBusy: boolean" in s
        assert "setCompactBusy: (busy: boolean) => void" in s
        assert "compactBusy: false" in s

    def test_actions_implemented(self):
        s = self._src()
        for impl in ("setTodos: (todos, counts) => set(",
                     "setQuestion: (q) => set(", "clearQuestion: () => set({ question: null })",
                     "updateSubagent: (agentId, patch) => set(",
                     "pushSubagent: (a) => set(", "clearSubagents: () => set({ subagents: [] })",
                     "setCompactBusy: (busy) => set({ compactBusy: busy })"):
            assert impl in s, f"缺实现：{impl}"


class TestWsHandlers:
    """ws.ts：八个新事件 + user_answer/compact 两个出站动作 + /api/todo 初始化。"""

    @staticmethod
    def _src() -> str:
        return _web("ws.ts")

    def test_todo_events(self):
        s = self._src()
        assert "case 'todo_updated'" in s
        assert "panel().setTodos(todos, data.counts || undefined)" in s
        # 初始化/对账走 REST（按会话持久化，刷新后仍能看到）
        assert "apiGet(`/todo?session_id=${encodeURIComponent(chatSid())}`)" in s
        assert "void loadTodos()" in s

    def test_question_events(self):
        s = self._src()
        for case in ("case 'user_question'", "case 'user_question_resolved'", "case 'user_question_stale'"):
            assert case in s, f"缺事件处理：{case}"
        assert "panel().setQuestion({" in s
        assert "panel().clearQuestion()" in s
        assert "sendUserAnswer" in s

    def test_presented_files_becomes_deliverable(self):
        s = self._src()
        assert "case 'presented_files'" in s
        assert "kind: 'deliverable'" in s
        assert "chat().append(taskMode(), {" in s

    def test_compact_result_feedback(self):
        s = self._src()
        assert "case 'compact_result'" in s
        assert "panel().setCompactBusy(false)" in s
        # 必须告诉用户压了多少；失败也要说清楚原因
        assert "已压缩上下文" in s
        assert "压缩未完成" in s

    def test_subagent_events(self):
        s = self._src()
        for case in ("case 'subagent_started'", "case 'subagent_update'", "case 'subagent_done'"):
            assert case in s, f"缺子智能体事件：{case}"
        assert "panel().pushSubagent({" in s
        assert "output_tail: String(data.output_tail)" in s
        assert "panel().updateSubagent(String(data.agent_id || '')" in s

    def test_task_start_clears_stale_state(self):
        s = self._src()
        start = s.index("case 'task_start'")
        tail = s[start:start + 2000]
        assert "panel().clearQuestion();" in tail
        assert "panel().clearSubagents();" in tail

    def test_outbound_actions(self):
        s = self._src()
        # 客户端 → 服务端契约：action 名与字段名一个都不能差
        assert "export function sendUserAnswer(" in s
        assert "action: 'user_answer', session_id: chatSid(), question_id: questionId, answers," in s
        assert "export function sendCompact(): boolean" in s
        assert "action: 'compact', session_id: chatSid()" in s


class TestUserQuestionModal:
    """components/modals/UserQuestionModal.tsx：选项 / 多选 / 自定义 / 超时提示。"""

    @staticmethod
    def _src() -> str:
        return _web("components/modals/UserQuestionModal.tsx")

    def test_renders_question_kinds(self):
        s = self._src()
        assert "export default function UserQuestionModal" in s
        assert "Checkbox" in s and "Radio" in s, "单选/多选都要支持"
        assert "Input.TextArea" in s, "需要自定义输入"
        assert "q.multi" in s
        assert "question_id" in s

    def test_submit_and_timeout(self):
        s = self._src()
        assert "sendUserAnswer(question.question_id, payload)" in s
        assert "clearQuestion()" in s
        assert "timeout_s" in s
        assert "超时" in s, "超时后果必须写清楚（不等于同意）"

    def test_mounted_in_app(self):
        app = _web("App.tsx")
        assert "import UserQuestionModal from './components/modals/UserQuestionModal'" in app
        assert "<UserQuestionModal />" in app


class TestDeliverableCard:
    """presented_files → ChatItem kind 'deliverable' → 可点开的交付卡片。"""

    def test_chat_item_kind(self):
        s = _web("store/chat.ts")
        assert "kind: 'deliverable'" in s
        assert "files: { path: string; name?: string; description?: string }[]" in s

    def test_bubble_actions(self):
        s = _web("components/chat/Bubbles.tsx")
        assert "export const DeliverableBubble = memo(" in s
        assert "交付文件" in s
        assert "openPreview({ url: `/api/preview/file?path=" in s, "HTML/图片应走预览"
        assert "openInCode(path)" in s, "其它文件应在代码标签打开"
        assert "copyText(path)" in s, "复制路径按钮"
        assert "dlv-card" in s

    def test_rendered_by_chat_panel(self):
        s = _web("components/chat/ChatPanel.tsx")
        assert "DeliverableBubble" in s
        assert "case 'deliverable': return <DeliverableBubble" in s


class TestCompactCommand:
    """/compact 指令：命令目录 → ChatPanel 分派 → WS 发送。"""

    def test_command_registered(self):
        s = _web("lib/commands.ts")
        assert "['compact'," in s
        assert "压缩当前上下文" in s
        assert "'compact']" in s, "动作名必须与 onPick 分派一致"

    def test_dispatched(self):
        s = _web("components/chat/ChatPanel.tsx")
        assert "case 'compact': sendCompact(); break;" in s
        assert "sendCompact" in s


class TestRightPanelSections:
    """右栏：「📋 待办」+「🤖 子智能体」（后者按 multi_agent 门控）。"""

    @staticmethod
    def _panel() -> str:
        return _web("components/right/RightPanel.tsx")

    def test_todo_section(self):
        s = self._panel()
        assert "📋 待办" in s
        assert "todo-row" in s and "TODO_ICON" in s
        assert "模型会维护一份待办清单" in s, "无待办时要说明清单从哪来"
        assert "apiDelete(`/todo?session_id=" in s, "清空要删服务端，否则重连又回来"

    def test_subagent_section_gated(self):
        s = self._panel()
        assert "🤖 子智能体" in s
        assert "featureOn('multi_agent')" in s, "专业版功能必须按特性开关显示"
        assert "SUBAGENT_LABEL" in s
        assert "output_tail || a.output" in s, "输出尾部"

    def test_summary_tab_also_shows(self):
        s = _web("components/right/SummaryTab.tsx")
        assert "📋 待办" in s
        assert "featureOn('multi_agent')" in s
        assert "sa-card" in s or "sa-tail" in s

    def test_shared_labels(self):
        s = _web("components/right/panelLabels.ts")
        assert "export const TODO_ICON" in s
        assert "export const SUBAGENT_LABEL" in s
        assert "export const subagentTone" in s


class TestReactCss:
    def test_styles_present(self):
        css = _web("global.css")
        for cls in (".todo-row", ".sa-card", ".dlv-card", ".uq-item", ".uq-opts", ".uq-countdown"):
            assert cls in css, f"缺样式：{cls}"


class TestLegacyPanel:
    """经典界面（/legacy）：index.html + chat.js + ws.js + panels.css 同步。"""

    def test_html_regions(self):
        html = _static("index.html")
        for marker in ('id="todo-section"', 'id="todo-list"', 'id="todo-counts"',
                       'id="subagents-section"', 'id="subagents-list"',
                       'id="deliverables-list"',
                       'id="question-modal"', 'id="question-content"', 'id="uq-countdown"'):
            assert marker in html, f"index.html 缺：{marker}"

    def test_chat_js_helpers(self):
        s = _static("js/chat.js")
        for fn in ("function renderTodo(", "async function refreshTodo(",
                   "async function clearTodo(", "function renderPresented(",
                   "function openDeliverable(", "function showUserQuestion(",
                   "function submitUserAnswer(", "function closeUserQuestion(",
                   "function renderSubagents(", "function onSubagentStarted(",
                   "function onSubagentUpdate(", "function onSubagentDone("):
            assert fn in s, f"chat.js 缺：{fn}"
        assert "sendUserAnswer(d.question_id, answers)" in s

    def test_ws_js_cases_and_actions(self):
        s = _static("js/ws.js")
        for case in ("case 'todo_updated'", "case 'user_question'",
                     "case 'user_question_resolved'", "case 'presented_files'",
                     "case 'compact_result'", "case 'subagent_started'",
                     "case 'subagent_update'", "case 'subagent_done'"):
            assert case in s, f"ws.js 缺事件分支：{case}"
        # user_answer 发送：action 名 + 会话 + 问题 id + 回答数组
        assert "action: 'user_answer'" in s
        assert "session_id: chatSid()" in s
        assert "question_id: questionId" in s
        assert "refreshTodo()" in s, "连上/终态后应主动对账待办"

    def test_panels_css(self):
        css = _static("css/panels.css")
        for cls in (".todo-row", ".sa-card", ".dlv-card", ".uq-item", ".uq-opts"):
            assert cls in css, f"panels.css 缺样式：{cls}"


@pytest.mark.skipif(not (DIST / "index.html").exists(), reason="前端未构建")
class TestShippedBundle:
    """构建产物里确实带上了新界面（防止只改源码、忘了统一构建就发版）。"""

    @staticmethod
    def _bundle() -> str:
        parts = []
        for pattern in ("*.js", "*.css"):
            for p in sorted((DIST / "assets").glob(pattern)):
                parts.append(p.read_text(encoding="utf-8", errors="ignore"))
        assert parts, "dist/assets 下应有构建产物"
        return "\n".join(parts)

    def test_features_in_bundle(self):
        b = self._bundle()
        for kw in ("待办", "子智能体", "交付文件", "模型需要你确认", "压缩当前上下文", "todo_updated"):
            assert kw in b, f"产物缺关键字：{kw}"
