"""v1.4 体验增强的回归 —— 字号 / 无闪烁切换 / 快捷键 / 任务完成通知。

前端没有 JS 测试栈，故这里从**源码与构建产物**两侧做结构性断言：能挡住
"整块功能被误删/改名后无人察觉"，以及本版真实踩到的两类坑：
    1. 文档写了、代码里却没有的快捷键（v1.4 前手册就写着 Ctrl+. 与 Ctrl+L，
       但从未实现，照着按的人只会以为软件坏了）；
    2. 字号基准被改成 16px 以外的值 —— 那会让"标准"档悄悄改变所有老用户的界面。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web" / "src"
DIST = ROOT / "automind" / "static" / "dist"
MANUAL_MD = ROOT / "使用手册.md"


def _src(rel: str) -> str:
    return (WEB / rel).read_text(encoding="utf-8")


class TestFontScale:
    def test_base_is_browser_default(self):
        """基准必须是 16px：全站没给 html/body 设过 font-size，一直吃浏览器默认。
        改成别的值，"标准"档就会悄悄缩放所有老用户的界面。"""
        assert re.search(r"FONT_BASE_PX\s*=\s*16\b", _src("store/prefs.ts"))

    def test_range_is_clamped(self):
        s = _src("store/prefs.ts")
        assert "FONT_MIN = 0.85" in s and "FONT_MAX = 1.3" in s
        # setFontScale 必须钳制，否则快捷键连按会把界面推到不可用
        assert "Math.min(FONT_MAX, Math.max(FONT_MIN" in s

    def test_antd_font_scales_together(self):
        """antd 用 px token，不随根字号走；不同步缩放会造成组件与自有样式割裂。"""
        assert "ANTD_BASE_FONT" in _src("store/prefs.ts")
        assert "ANTD_BASE_FONT * fontScale" in _src("App.tsx")

    def test_persisted(self):
        assert "automind_font_scale" in _src("store/prefs.ts")


class TestNoFlashOnViewSwitch:
    def test_chat_panel_stays_mounted(self):
        """黑屏闪烁的根因就是切视图时整体卸载 ChatPanel。
        必须保持挂载、只切显示 —— 出现三元卸载写法即回归。"""
        app = _src("App.tsx")
        assert "view-slot" in app
        assert re.search(r"display:\s*ViewComp\s*\?\s*'none'\s*:\s*'flex'", app), \
            "ChatPanel 应常驻挂载、仅切换显示"
        assert not re.search(r"ViewComp\s*\?\s*\(\s*<div[^>]*>\s*<ViewComp\s*/>\s*</div>\s*\)\s*:\s*\(\s*<ChatPanel", app), \
            "不得回退成'切视图即卸载 ChatPanel'的写法"

    def test_reduce_motion_supported(self):
        css = (WEB / "global.css").read_text(encoding="utf-8")
        assert ".reduce-motion" in css
        assert "prefers-reduced-motion" in css, "系统级减少动效偏好也应被尊重"


class TestHotkeys:
    def test_registry_drives_help_modal(self):
        """帮助弹窗必须由注册表生成，否则迟早出现'写了但按不动'。"""
        assert "HOTKEYS" in _src("components/modals/ShortcutsModal.tsx")

    def test_ime_composition_not_hijacked(self):
        # 中文输入法组词中抢键会让人打不出字
        assert "isComposing" in _src("lib/hotkeys.ts")

    def test_plain_char_keys_blocked_in_input(self):
        s = _src("lib/hotkeys.ts")
        assert "isTypingTarget" in s and "inInput" in s

    def test_documented_legacy_aliases_are_implemented(self):
        """v1.4 前手册承诺过 Ctrl+. / Ctrl+L，必须真的能用。"""
        s = _src("lib/hotkeys.ts")
        assert "alias: 'mod+L'" in s, "手册承诺的 Ctrl+L（新会话）应作为别名支持"
        assert "alias: 'mod+.'" in s, "手册承诺的 Ctrl+. （中断）应作为别名支持"

    def test_stop_uses_websocket_not_missing_http_route(self):
        """后端没有 /api/stop 路由，走 HTTP 会静默 404、任务停不下来。"""
        app = _src("App.tsx")
        assert "sendStop" in app
        assert "apiPost('/stop'" not in app


class TestManualMatchesImplementation:
    """手册里写的快捷键必须真实存在 —— 正是 v1.4 前踩到的那个文档 bug。"""

    @staticmethod
    def _implemented() -> set[str]:
        s = _src("lib/hotkeys.ts")
        combos = re.findall(r"(?:combo|alias):\s*'([^']+)'", s)
        out = set()
        for c in combos:
            out.add(c.lower().replace("mod", "ctrl").replace(" ", ""))
        return out

    @pytest.mark.skipif(not MANUAL_MD.exists(), reason="手册缺失")
    def test_no_phantom_shortcuts_in_manual(self):
        text = MANUAL_MD.read_text(encoding="utf-8")
        section = text.split("### 键盘快捷键", 1)
        assert len(section) == 2, "手册应有键盘快捷键章节"
        body = section[1].split("\n## ", 1)[0]

        impl = self._implemented()
        documented = {c.lower().replace(" ", "")
                      for c in re.findall(r"`(Ctrl\+[^`]+)`", body)}
        # 手册用 ~ 表示区间（如 Ctrl+1 ~ Ctrl+3），逐个展开不现实，按前缀放行
        phantom = []
        for d in documented:
            if d in impl:
                continue
            if any(d.startswith(p) for p in ("ctrl+1", "ctrl+2", "ctrl+3", "ctrl+=", "ctrl+-", "ctrl+0")):
                continue
            phantom.append(d)
        assert not phantom, f"手册写了但代码里不存在的快捷键：{sorted(phantom)}"

    @pytest.mark.skipif(not MANUAL_MD.exists(), reason="手册缺失")
    def test_manual_documents_new_prefs(self):
        text = MANUAL_MD.read_text(encoding="utf-8")
        for kw in ("界面偏好", "字号", "任务完成通知", "减少动效"):
            assert kw in text, f"手册应说明「{kw}」"


class TestNotifications:
    def test_only_when_window_hidden(self):
        """用户正看着界面时再弹系统通知纯属打扰。"""
        s = _src("lib/notify.ts")
        assert "windowHidden" in s
        assert "visibilityState" in s and "hasFocus" in s

    def test_permission_requested_on_user_gesture(self):
        # 浏览器要求权限申请由用户手势触发；挂在设置开关上而非启动时偷弹
        assert "requestNotifyPermission" in _src("components/modals/SettingsModals.tsx")

    def test_all_terminal_states_notify(self):
        ws = _src("ws.ts")
        assert ws.count("notifyDone(") >= 4, "完成/失败/中断/对话结束都应通知"
        assert "usePrefs.getState().notifyOnDone" in ws, "应受设置开关控制"


@pytest.mark.skipif(not (DIST / "index.html").exists(), reason="前端未构建")
class TestShippedBundle:
    """构建产物里确实带上了这些功能（防止只改源码、忘了重新构建就发版）。"""

    @staticmethod
    def _bundle() -> str:
        js = sorted((DIST / "assets").glob("*.js"))
        assert js, "dist/assets 下应有构建产物"
        return "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in js)

    def test_features_present_in_bundle(self):
        b = self._bundle()
        assert "automind_font_scale" in b, "字号偏好未进产物"
        assert "automind_notify_done" in b, "通知偏好未进产物"
        assert "view-slot" in b, "无闪烁切换未进产物"
        assert "键盘快捷键" in b, "快捷键帮助未进产物"
