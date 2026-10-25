"""file_write 磁盘回读校验 —— 中文文件名/内容一次写入即完成。

背景：测评报告里 ``smoke_chinese_content`` 两条赛道都 Token 超限（功能断言全过）。
根因不是写错，而是模型写完不放心，又用 file_read / terminal 复核 —— 每一步都要
重发全量上下文，简单任务被拖到 12 万 token 以上。

因此这里锁定三个契约：
1. 成功回执**自带磁盘回读结论**（verified / readback_matches / size_bytes /
   encoding / preview），模型看到 verified=true 就没有理由再验证一次；
2. 回执里的 ``note`` 明确写出"不需要再 file_read/terminal 复核"；
3. 中文文件名 + 中文内容、非 UTF-8 编码、覆盖写都走通，且既有字段
   （path/size/created/overwritten/content_hash）与失败路径行为不变。

全部用 ``tmp_path`` 作为 project_root —— 不碰真实运行目录。
"""

from __future__ import annotations

import pytest

from automind.tools.file_editor import JOURNAL, FileReadTool, FileWriteTool
from automind.tools.write_guard import content_hash

#: 任务里点名要覆盖的中文内容（7 字符 / UTF-8 21 字节）
CN_CONTENT = "你好，自动化。"


@pytest.fixture(autouse=True)
def _clean_journal():
    """改动日志是进程级单例，避免本文件的写入串到别的用例的账本里。"""
    JOURNAL.clear()
    yield
    JOURNAL.clear()


# ═══════════════════════════════════════════════════════════
# 1. 中文文件名 + 中文内容：一次写入即带校验结论
# ═══════════════════════════════════════════════════════════


async def test_chinese_filename_and_content_verified(tmp_path):
    tool = FileWriteTool(project_root=tmp_path)
    r = await tool.execute(path="子目录/备注.txt", content=CN_CONTENT)
    assert r.success, r.error
    out = r.output

    assert out["verified"] is True
    assert out["readback_matches"] is True
    assert out["encoding"] == "utf-8"
    # 关键：磁盘字节数（UTF-8 下一个汉字 3 字节），不是字符数
    assert out["size_bytes"] == len(CN_CONTENT.encode("utf-8"))
    assert out["chars"] == len(CN_CONTENT)
    assert out["preview"] == CN_CONTENT
    assert "readback_warning" not in out

    # 中文目录 + 文件名真的落到了磁盘，内容按 UTF-8 可读回
    target = tmp_path / "子目录" / "备注.txt"
    assert target.read_text(encoding="utf-8") == CN_CONTENT

    # 回执要明确劝阻"再验证一次"—— 这是 token 超限的直接来源
    assert "回读校验" in out["note"]
    assert "不需要" in out["note"]


async def test_preview_is_first_80_chars(tmp_path):
    """preview 固定前 80 字符：够模型确认写对了，又不把长内容塞回上下文。"""
    content = "你" * 200
    r = await FileWriteTool(project_root=tmp_path).execute(path="long.txt",
                                                          content=content)
    assert r.success, r.error
    assert r.output["preview"] == content[:80]
    assert len(r.output["preview"]) == 80


async def test_none_encoding_falls_back_to_utf8(tmp_path):
    """模型偶尔显式传 encoding=None；不能退回中文 Windows 的 locale 编码。"""
    r = await FileWriteTool(project_root=tmp_path).execute(
        path="none.txt", content="中文", encoding=None)
    assert r.success, r.error
    assert r.output["encoding"] == "utf-8"
    assert r.output["verified"] is True
    assert (tmp_path / "none.txt").read_text(encoding="utf-8") == "中文"


# ═══════════════════════════════════════════════════════════
# 2. 覆盖写 / 非 UTF-8 编码：回读一致
# ═══════════════════════════════════════════════════════════


async def test_overwrite_verified_and_flags_flip(tmp_path):
    tool = FileWriteTool(project_root=tmp_path)
    first = await tool.execute(path="note.txt", content="第一版")
    assert first.success and first.output["created"] is True
    assert first.output["overwritten"] is False

    second_content = "第二版：覆盖后的中文内容"
    second = await tool.execute(path="note.txt", content=second_content)
    assert second.success, second.error
    out = second.output
    assert out["created"] is False
    assert out["overwritten"] is True
    assert out["verified"] is True
    assert out["readback_matches"] is True
    assert out["size_bytes"] == len(second_content.encode("utf-8"))
    assert out["size_bytes"] == (tmp_path / "note.txt").stat().st_size
    assert (tmp_path / "note.txt").read_text(encoding="utf-8") == second_content


async def test_gb18030_round_trip_verified(tmp_path):
    """老中文文件按 gb18030 写：回执按实际编码校验，不能按 UTF-8 误报。"""
    content = "中文内容测试 ABC 123"
    r = await FileWriteTool(project_root=tmp_path).execute(
        path="gbk.txt", content=content, encoding="gb18030")
    assert r.success, r.error
    out = r.output
    assert out["encoding"] == "gb18030"
    assert out["verified"] is True
    assert out["readback_matches"] is True
    assert out["size_bytes"] == len(content.encode("gb18030"))
    # 磁盘上的字节必须是真的 gb18030（而不是被悄悄改成 UTF-8）
    assert (tmp_path / "gbk.txt").read_bytes().decode("gb18030") == content


async def test_empty_content_verified(tmp_path):
    """空文件是合法写入，不能因为大小 0 就判成校验失败。"""
    r = await FileWriteTool(project_root=tmp_path).execute(path="empty.txt",
                                                          content="")
    assert r.success, r.error
    out = r.output
    assert out["size_bytes"] == 0
    assert out["chars"] == 0
    assert out["preview"] == ""
    assert out["verified"] is True


async def test_crlf_content_not_reported_as_mismatch(tmp_path):
    """内容自带 CRLF 时，平台换行翻译不能被误报成"回读不一致"。

    Windows 上 ``write_text`` 会把 ``\\n`` 翻译成 CRLF、``read_text`` 再归一化；
    直接拿归一化后的文本比对，显式 CRLF 内容会被二次翻译成假失败 —— 那会逼模型
    重新读一遍文件，正是我们要消除的浪费。
    """
    content = "第一行\r\n第二行"
    r = await FileWriteTool(project_root=tmp_path).execute(
        path="win.txt", content=content)
    assert r.success, r.error
    assert r.output["readback_matches"] is True
    assert r.output["verified"] is True
    assert "readback_warning" not in r.output


async def test_multiline_content_verified_and_size_bytes_is_disk_size(tmp_path):
    """多行内容的 size_bytes 取磁盘真实大小（Windows 上含 CRLF 的额外字节）。"""
    content = "第一行\n第二行\n"
    r = await FileWriteTool(project_root=tmp_path).execute(
        path="multi.txt", content=content)
    assert r.success, r.error
    assert r.output["verified"] is True
    assert r.output["size_bytes"] == (tmp_path / "multi.txt").stat().st_size
    assert (tmp_path / "multi.txt").read_text(encoding="utf-8") == content


# ═══════════════════════════════════════════════════════════
# 3. 既有字段 / 失败路径不回归
# ═══════════════════════════════════════════════════════════


async def test_legacy_fields_and_hash_unchanged(tmp_path):
    """path/size/created/overwritten/content_hash 语义保持不变（既有测试依赖）。"""
    content = "x = 1\nprint(x)\n"
    r = await FileWriteTool(project_root=tmp_path).execute(
        path="legacy.py", content=content)
    assert r.success, r.error
    out = r.output
    assert out["path"] == str((tmp_path / "legacy.py").resolve())
    assert out["size"] == len(content)                 # 既有字段仍是字符数
    assert out["created"] is True
    assert out["overwritten"] is False
    assert out["content_hash"] == content_hash(content)

    # 与 file_read 的指纹口径一致：模型可用它做后续 file_write 的 expected_hash
    rd = await FileReadTool(project_root=tmp_path).execute(path="legacy.py")
    assert rd.success and rd.output["content"] == content
    assert rd.output["content_hash"] == out["content_hash"]


async def test_non_string_content_serialized_and_verified(tmp_path):
    """对象/数组内容先序列化再写（既有行为），回读校验同样生效。"""
    r = await FileWriteTool(project_root=tmp_path).execute(
        path="a.json", content={"k": "值"})
    assert r.success, r.error
    assert r.output["verified"] is True
    assert r.output["chars"] == len((tmp_path / "a.json").read_text(encoding="utf-8"))


async def test_directory_target_and_bad_encoding_still_fail(tmp_path):
    """失败路径行为不变：返回 success=False，且不携带成功回执字段。"""
    tool = FileWriteTool(project_root=tmp_path)
    (tmp_path / "adir").mkdir()

    r = await tool.execute(path="adir", content="x")
    assert r.success is False
    assert "目录" in r.error
    assert "verified" not in (r.output or {})

    r = await tool.execute(path="bad.txt", content="x", encoding="no-such-encoding")
    assert r.success is False
    assert "unknown encoding" in r.error
    # 既有行为：write_text 先打开文件再解析编码名，非法编码会留下一个空文件。
    # 这里只确认"没把内容写进去"、失败语义不变，不顺手改这条老路径。
    leftover = tmp_path / "bad.txt"
    assert not leftover.exists() or leftover.stat().st_size == 0
    assert "verified" not in (r.output or {})


async def test_path_outside_project_root_still_rejected(tmp_path):
    """project_root 之外的路径仍按既有规则拒绝，且不会真的落盘。"""
    project = tmp_path / "proj"
    project.mkdir()
    r = await FileWriteTool(project_root=project).execute(
        path="../escape.txt", content="x")
    assert r.success is False
    assert "越界" in r.error
    assert not (tmp_path / "escape.txt").exists()


# ═══════════════════════════════════════════════════════════
# 4. 工具描述（schema 是每轮都发给模型的固定成本，必须写到点上）
# ═══════════════════════════════════════════════════════════


def test_description_documents_unicode_and_disk_readback():
    desc = FileWriteTool.description
    assert "Unicode" in desc and "Chinese" in desc
    assert "back from disk" in desc and "verified=true" in desc
    assert "no extra file_read or terminal" in desc
