"""知识库自动注入不再每轮重付一遍。

v1.6.2 及更早：`_apply_kb` 把检索到的片段拼在用户这句话前面发出去，生成完
再用 `_restore_kb_history` 把历史里那条还原成用户原话。于是——

    · 模型从第二轮起就看不见第一轮的片段了（被还原掉了），
    · 所以每一轮都必须重新注入，
    · 同一份文档的 500~900 token **连问五轮就付五遍**。

修复后片段作为独立的 system 消息留在历史里，并按"是否已在窗口内"逐条去重：
同一段只付一次；换了话题检索到新片段时才注入新的那几段。
"""

from __future__ import annotations

import pytest

srv = pytest.importorskip("automind.server")


class _FakeStore:
    """按固定顺序吐检索结果的知识库替身。"""

    def __init__(self, batches):
        self._batches = list(batches)
        self.searches = 0

    def doc_count(self):
        return 3

    def search(self, query, **kw):
        self.searches += 1
        return self._batches.pop(0) if self._batches else []

    def log_search(self, *a, **kw):
        pass


def _hit(name, seq, text):
    return {"text": text, "score": 0.9, "doc_id": "d1",
            "doc_name": name, "seq": seq, "kb": "default"}


@pytest.fixture()
def kb(monkeypatch):
    """把 _apply_kb 的三个外部依赖换成可控替身。"""
    def _install(batches, edition_features=()):
        store = _FakeStore(batches)
        monkeypatch.setattr(srv, "_kb_store", lambda: store)
        monkeypatch.setattr(srv, "_read_config", lambda: {"kb_auto": True})
        monkeypatch.setattr(srv._edition, "has_feature",
                            lambda f: f in edition_features)
        return store
    return _install


def test_first_turn_injects_and_keeps_the_user_message_intact(kb):
    """首轮要注入；但用户那句话必须**原样**留着。"""
    kb([[_hit("手册.md", 0, "报销流程：先在系统提单，再交主管审批。")]])
    hist: list = []

    out = srv._apply_kb("报销怎么走？", hist)

    assert out == "报销怎么走？", "用户原话被改写了 —— 历史里会显示成不是他说的话"
    assert len(hist) == 1 and hist[0]["role"] == "system"
    assert "报销流程" in hist[0]["content"]


def test_same_chunk_is_not_injected_twice(kb):
    """同一段已经在窗口里 → 第二轮一个 token 都不该重付。"""
    chunk = _hit("手册.md", 0, "报销流程：先在系统提单，再交主管审批。")
    store = kb([[chunk], [chunk]])
    hist: list = []

    srv._apply_kb("报销怎么走？", hist)
    hist.append({"role": "user", "content": "报销怎么走？"})
    hist.append({"role": "assistant", "content": "先提单再审批。"})
    before = len(hist)

    srv._apply_kb("那审批要多久？", hist)

    assert store.searches == 2, "检索本身照做（要判断有没有新内容）"
    assert len(hist) == before, "同一段片段被重复注入了 —— 这就是重复付费"


def test_new_chunks_still_get_injected(kb):
    """换了话题、检索到新片段时当然要注入 —— 去重不能变成"不再检索"。"""
    old = _hit("手册.md", 0, "报销流程：先在系统提单，再交主管审批。")
    new = _hit("考勤.md", 2, "调休需在当月内使用，跨月自动作废。")
    kb([[old], [new]])
    hist: list = []

    srv._apply_kb("报销怎么走？", hist)
    hist.append({"role": "user", "content": "报销怎么走？"})
    srv._apply_kb("调休规则是什么？", hist)

    joined = " ".join(m["content"] for m in hist if m["role"] == "system")
    assert "调休需在当月内使用" in joined, "新片段没注入，回答会缺依据"


def test_partially_new_hits_inject_only_the_new_part(kb):
    """三条命中里只有一条是新的 → 只发那一条。"""
    a = _hit("手册.md", 0, "AAA 报销流程说明，第一段内容足够长以便指纹稳定。")
    b = _hit("手册.md", 1, "BBB 审批时限说明，第二段内容足够长以便指纹稳定。")
    kb([[a], [a, b]])
    hist: list = []

    srv._apply_kb("报销？", hist)
    hist.append({"role": "user", "content": "报销？"})
    srv._apply_kb("审批时限？", hist)

    latest = [m for m in hist if m["role"] == "system"][-1]["content"]
    assert "BBB 审批时限说明" in latest
    assert "AAA 报销流程说明" not in latest, "已经在场的那段又发了一遍"


def test_chunk_scrolled_out_of_the_window_is_re_injected(kb):
    """片段被挤出模型可见窗口后必须重新注入 —— 否则回答会凭空少了依据。"""
    chunk = _hit("手册.md", 0, "报销流程：先在系统提单，再交主管审批。")
    kb([[chunk], [chunk]])
    hist: list = []

    srv._apply_kb("报销怎么走？", hist)
    # 之后聊了一大堆别的，早就把那条 system 挤出 hist[-20:] 了
    for i in range(30):
        hist.append({"role": "user", "content": f"闲聊 {i}"})
        hist.append({"role": "assistant", "content": f"好的 {i}"})
    before = len(hist)

    srv._apply_kb("报销怎么走来着？", hist)

    assert len(hist) == before + 1, "模型已经看不到那段了，却没有补发"


def test_auto_retrieval_switch_is_respected(kb, monkeypatch):
    """关掉「对话中自动检索」就不该再碰知识库。"""
    store = kb([[_hit("手册.md", 0, "任何内容")]])
    monkeypatch.setattr(srv, "_read_config", lambda: {"kb_auto": False})
    hist: list = []

    assert srv._apply_kb("随便问问", hist) == "随便问问"
    assert store.searches == 0 and hist == []


def test_legacy_callers_without_history_still_work(kb):
    """没有历史可挂的调用方（旧签名）退回"拼进本轮提问"，不能直接失效。"""
    kb([[_hit("手册.md", 0, "报销流程说明。")]])
    out = srv._apply_kb("报销怎么走？")
    assert "报销流程说明" in out and out.endswith("报销怎么走？")


def test_store_failure_never_breaks_the_chat(kb, monkeypatch):
    """知识库挂了只是没有参考资料，不能把整个对话一起带崩。"""
    class _Boom:
        def doc_count(self):
            raise RuntimeError("库文件损坏")

    monkeypatch.setattr(srv, "_kb_store", _Boom)
    monkeypatch.setattr(srv, "_read_config", lambda: {"kb_auto": True})
    hist: list = []
    assert srv._apply_kb("报销怎么走？", hist) == "报销怎么走？"
    assert hist == []
