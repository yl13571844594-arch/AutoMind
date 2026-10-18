"""v1.11.10 前端：右栏实时「缓存命中率 / 上下文使用率」回归。

前端没有 JS 测试栈，故沿用 v1.4 起的老办法：从**源码与构建产物**两侧做结构性断言。
这一版真正想盯住的是三件事：

    1. 固定契约别悄悄漂移 —— WS ``usage_update`` 的 cumulative/context 字段与
       ``GET /api/tokens`` 的新字段，UI 侧必须按同一口径读（字段名错一个，
       界面上只会安静地显示「—」，没有任何报错能提醒你）；
    2. 「没数据」与「0%」必须区分 —— 模型不返回缓存统计时 cache_rate 是 null，
       界面要显示「—」；如果把 null 落成 0，用户会以为"缓存一次都没命中"；
    3. 经典兜底界面（/legacy）不能掉队 —— React 构建失败时它就是唯一能看的界面，
       它缺了这两项，等于"这个功能在兜底路径上不存在"。
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


class TestLiveUsageStore:
    """store/panel.ts：实时用量单独一份状态（不复用任务终态的 TaskStats）。"""

    def test_live_usage_shape(self):
        s = _web("store/panel.ts")
        assert "export interface LiveUsage" in s
        # 契约字段一个都不能少：WS cumulative + context 的映射目标
        for field in (
            "input_tokens: number",
            "cached_tokens: number",
            "calls: number",
            "cache_hit_calls: number",
            "cache_rate: number | null",
            "peak_prompt_tokens: number",
            "context_max: number",
            "context_pct: number",
            "active: boolean",
        ):
            assert field in s, f"LiveUsage 缺字段：{field}"

    def test_nullable_cache_rate_documented(self):
        """cache_rate 为 null 的语义必须在源码里说清楚，否则后来人很容易改成 0。"""
        s = _web("store/panel.ts")
        assert "提供商没给缓存统计" in s

    def test_set_and_reset_actions(self):
        s = _web("store/panel.ts")
        assert "setUsage: (u: Partial<LiveUsage>) => void" in s
        assert "resetUsage: () => void" in s
        assert "usage: LiveUsage;" in s, "PanelState 必须声明 usage 字段"
        assert "setUsage: (u) => set(" in s
        assert "resetUsage: () => set({ usage: EMPTY_USAGE })" in s
        assert "usage: EMPTY_USAGE" in s, "初始值应为全零且 active=false"


class TestWsUsageUpdate:
    """ws.ts：task_start 归零；usage_update 读取 cumulative + context。"""

    def test_task_start_resets_usage(self):
        s = _web("ws.ts")
        assert "panel().resetUsage();" in s, "新任务开始必须清掉上一轮命中率"

    def test_usage_update_reads_contract_fields(self):
        s = _web("ws.ts")
        assert "case 'usage_update'" in s
        assert "panel().setUsage({" in s
        for field in (
            "input_tokens: num(cum.input_tokens)",
            "cached_tokens: num(cum.cached_tokens)",
            "calls: num(cum.calls)",
            "cache_hit_calls: num(cum.cache_hit_calls)",
            "peak_prompt_tokens: num(cum.peak_prompt_tokens)",
            "context_max: num(ctx.max)",
            "context_pct: num(ctx.pct)",
        ):
            assert field in s, f"usage_update 未读取：{field}"
        # null 必须原样保留（界面据此显示「—」）
        assert "typeof cum.cache_rate === 'number' ? cum.cache_rate : null" in s

    def test_keeps_legacy_token_stat(self):
        """v1.5.1 的 stats.tokens 行为要保留，不能为了新面板把老指示弄丢。"""
        assert "panel().setStats({ tokens: cum.total_tokens });" in _web("ws.ts")


class TestReactResetPoints:
    """会话切换 / 清空会话时同步归零，否则新会话挂着旧对话的命中率。"""

    def test_conversation_switch_resets(self):
        s = _web("store/conv.ts")
        assert "resetPanels()" in s
        assert "usePanel.getState().resetUsage();" in s

    def test_clear_session_resets(self):
        assert "usePanel.getState().resetUsage();" in _web("components/Header.tsx")


class TestObservePanel:
    """RightPanel.tsx：「⚡ 缓存与上下文」区块与说明行。"""

    @staticmethod
    def _src() -> str:
        return _web("components/right/RightPanel.tsx")

    def test_section_exists_and_positioned(self):
        s = self._src()
        assert "⚡ 缓存与上下文" in s
        # 放在「📊 实时状态」之后、「🪙 Token 用量统计」之前
        live = s.index("📊 实时状态")
        block = s.index("⚡ 缓存与上下文")
        tokens = s.index("🪙 Token 用量统计")
        assert live < block < tokens, "缓存区块应紧跟实时状态、位于 Token 统计之前"

    def test_both_metrics_rendered(self):
        s = self._src()
        assert "缓存命中率" in s
        assert "上下文使用率" in s
        assert "usage-bar" in s and "usage-bar-fill" in s, "上下文需要进度条"

    def test_no_data_renders_dash(self):
        """模型未提供缓存统计时显示「—」而不是 0%。"""
        s = self._src()
        assert "usage.rate == null ? '—'" in s
        assert "hasCtx ?" in s, "没有任何调用时上下文也应是「—」"

    def test_fallback_source_and_labels(self):
        s = self._src()
        assert "apiGet('/tokens')" in s, "无实时事件时应回退 /api/tokens 的会话值"
        assert "本次任务" in s and "本次运行" in s
        assert "usage.active" in s or "liveUsage.active" in s

    def test_explain_lines(self):
        s = self._src()
        assert "命中 {usage.cached.toLocaleString()} / 输入 {usage.input.toLocaleString()} tk" in s
        assert "次调用" in s
        assert "（取最近一次请求）" in s

    def test_color_levels(self):
        s = self._src()
        assert "usage.rate >= 50 ? ' ok' : usage.rate > 0 ? ' warn'" in s
        assert "' bad' : usage.pct >= 70 ? ' warn'" in s


class TestSummaryHeader:
    """SummaryTab.tsx 头部 meta：⚡ 缓存 xx% · 🧠 上下文 xx%。"""

    def test_meta_bits(self):
        s = _web("components/right/SummaryTab.tsx")
        assert "⚡ 缓存" in s
        assert "🧠 上下文" in s
        assert "usageBits" in s and "usage.active" in s


class TestReactCss:
    def test_usage_bar_styles(self):
        s = _web("global.css")
        assert ".usage-bar {" in s
        assert ".usage-bar-fill" in s
        assert ".stat-item .value.warn" in s
        assert ".stat-item .value.bad" in s


class TestLegacyPanel:
    """经典界面（/legacy）：index.html + chat.js + ws.js + panels.css 同步。"""

    @staticmethod
    def _live_section() -> str:
        """截取「📊 实时状态」这一节，避免把 Token 统计区的 id 误当成新面板。"""
        html = _static("index.html")
        return html.split("📊 实时状态", 1)[1].split("🪙 Token 用量统计", 1)[0]

    def test_stat_items_and_bar_ids(self):
        sec = self._live_section()
        assert 'id="stat-cache"' in sec, "缺缓存命中率数值位"
        assert 'id="stat-context"' in sec, "缺上下文使用率数值位"
        assert 'id="ctx-bar"' in sec and 'id="ctx-bar-fill"' in sec, "缺上下文进度条"
        assert "缓存命中率" in sec and "上下文使用率" in sec

    def test_chat_js_renders_usage(self):
        s = _static("js/chat.js")
        assert "function renderUsage(" in s
        assert "function applyUsageUpdate(" in s, "WS 实时入口"
        assert "function resetLiveUsage(" in s, "新任务开始清空"
        # refreshTokens 读 /api/tokens 的新字段（会话累计回退）
        for field in ("t.cache_rate", "t.cached", "t.input", "t.calls",
                      "ctx.used", "ctx.max", "ctx.pct"):
            assert field in s, f"refreshTokens 未使用：{field}"
        # 口径：null 显示「—」
        assert "rate == null ? '—'" in s

    def test_ws_js_dispatches_usage_update(self):
        s = _static("js/ws.js")
        assert "case 'usage_update':" in s
        assert "applyUsageUpdate(data)" in s
        # window 级函数存在于 chat.js，两个静态资源版本可能错配 —— 必须存在性判断
        assert "typeof applyUsageUpdate === 'function'" in s
        assert "typeof resetLiveUsage === 'function'" in s

    def test_panels_css(self):
        s = _static("css/panels.css")
        assert ".ctx-bar {" in s
        assert ".ctx-bar-fill" in s
        assert ".stat-item .value.bad" in s


@pytest.mark.skipif(not (DIST / "index.html").exists(), reason="前端未构建")
class TestShippedBundle:
    """构建产物里确实带上了新面板（防止只改源码、忘了统一构建就发版）。"""

    @staticmethod
    def _bundle() -> str:
        parts = []
        for pattern in ("*.js", "*.css"):
            for p in sorted((DIST / "assets").glob(pattern)):
                parts.append(p.read_text(encoding="utf-8", errors="ignore"))
        assert parts, "dist/assets 下应有构建产物"
        return "\n".join(parts)

    def test_panel_features_in_bundle(self):
        b = self._bundle()
        assert "缓存命中率" in b, "缓存命中率未进产物"
        assert "上下文使用率" in b, "上下文使用率未进产物"
        assert "⚡ 缓存与上下文" in b, "右栏缓存区块未进产物"
        assert "⚡ 缓存" in b and "🧠 上下文" in b, "摘要头部 meta 未进产物"
        assert "usage-bar" in b, "上下文进度条样式未进产物"
