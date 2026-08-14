"""v1.6.0 内置工具的"真的能用"验收。

注册上了、schema 漂亮，不等于能用。这里盯的是四个**不报错但结果是错的**
缺陷 —— 它们比崩溃更难被发现：

1. `git_tool` 用系统 ANSI 代码页解码 git 输出，中文提交信息触发
   UnicodeDecodeError 让读取线程静默死掉 → exit_code=0 但 output 是空串；
2. `git_tool` 未关 core.quotepath，中文文件名输出成 "\\344\\275\\277..." 八进制串；
3. `chart_tool` 只认 x/y，模型传 labels/values 时静默画出**空白图**并报 success；
4. `chart_tool` 用默认字体，中文标题渲染成一排空心方框。
"""

from __future__ import annotations

import subprocess
import warnings
from pathlib import Path

import pytest

from automind.tools._toolkit import OPTIONAL_DEPS  # noqa: F401  (确保工具模块可导入)
from automind.tools.media_tools import ChartTool
from automind.tools.office.ppt_tool import PptTool
from automind.tools.system_tools import GitTool

matplotlib = pytest.importorskip("matplotlib")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, encoding="utf-8", errors="replace")


@pytest.fixture
def cjk_repo(tmp_path):
    """一个文件名与提交信息都是中文的仓库 —— 中文用户的常态。"""
    repo = tmp_path / "仓库"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "测试用户")
    (repo / "使用手册.md").write_text("# 手册\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "初始提交：加入使用手册与《测试》文档")
    return repo


class TestGitToolEncoding:
    async def test_chinese_commit_message_survives(self, cjk_repo):
        r = await GitTool().execute(action="log", repo=str(cjk_repo), max_log=5)
        assert r.success
        out = r.output["output"]
        assert out.strip(), "中文提交信息把解码线程搞挂了 → 输出成了空串"
        assert "初始提交" in out
        assert "《测试》文档" in out

    async def test_chinese_filename_is_not_octal_escaped(self, cjk_repo):
        (cjk_repo / "新增文件.txt").write_text("x", encoding="utf-8")
        r = await GitTool().execute(action="status", repo=str(cjk_repo))
        assert r.success
        out = r.output["output"]
        assert "新增文件.txt" in out, f"中文文件名被转义了：{out!r}"
        assert "\\344" not in out


class TestChartToolCorrectness:
    @pytest.mark.parametrize("action", ["bar", "line", "scatter"])
    async def test_labels_values_aliases_match_canonical_x_y(self, tmp_path, action):
        """labels/values 必须画出与 x/y 完全相同的图。

        直接比对两张 PNG 是否逐字节相同 —— 比"文件大于 N 字节"这种阈值
        可靠得多：别名走错分支的结果是空白图，绝不可能与正确图一致。
        """
        alias, canon = tmp_path / "alias.png", tmp_path / "canon.png"
        a = await ChartTool().execute(action=action, labels=["甲", "乙", "丙"],
                                      values=[3, 1, 2], output=str(alias))
        c = await ChartTool().execute(action=action, x=["甲", "乙", "丙"],
                                      y=[3, 1, 2], output=str(canon))
        assert a.success and c.success, a.error or c.error
        assert alias.read_bytes() == canon.read_bytes(), \
            "labels/values 走了别的分支，画出来的图和 x/y 不一样"

    async def test_rendered_chart_is_not_blank(self, tmp_path):
        """兜底：画布上得真有东西（不是一整片背景色）。"""
        Image = pytest.importorskip("PIL.Image", reason="需要 pillow 才能读回像素")
        out = tmp_path / "c.png"
        r = await ChartTool().execute(action="bar", labels=["甲", "乙"],
                                      values=[3, 1], output=str(out))
        assert r.success, r.error
        colors = Image.open(out).convert("RGB").getcolors(maxcolors=1 << 20)
        assert colors and len(colors) > 8, "图上几乎只有背景色，等于空白图"

    @pytest.mark.parametrize("kw", [
        {},                                   # 两侧全空
        {"labels": ["甲", "乙"]},              # 只有 x
        {"values": [1, 2]},                   # 只有 y
    ])
    async def test_empty_series_errors_instead_of_blank_png(self, tmp_path, kw):
        out = tmp_path / "empty.png"
        r = await ChartTool().execute(action="bar", output=str(out), **kw)
        assert not r.success, "数据不全却报成功，用户拿到的是一张空白图"
        assert not out.exists(), "报错了就不该留下垃圾文件"

    async def test_mismatched_lengths_are_rejected(self, tmp_path):
        r = await ChartTool().execute(action="line", x=[1, 2, 3], y=[1, 2],
                                      output=str(tmp_path / "m.png"))
        assert not r.success
        assert "长度不一致" in (r.error or "")

    async def test_chinese_title_renders_without_missing_glyphs(self, tmp_path):
        """中文标题不能画成方框（matplotlib 会为此发 UserWarning）。"""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            r = await ChartTool().execute(
                action="bar", labels=["华东", "华北"], values=[6, 4],
                title="区域销量占比", xlabel="区域", ylabel="销量",
                output=str(tmp_path / "cjk.png"))
        assert r.success, r.error
        glyph = [str(w.message) for w in caught if "missing from font" in str(w.message)]
        assert not glyph, f"中文被渲染成方框：{glyph[:3]}"


class TestPptToolForgivingCreate:
    async def test_create_accepts_top_level_title_and_bullets(self, tmp_path):
        """title/bullets 是 add_slide 的形状，但模型很常拿它调 create。"""
        pytest.importorskip("pptx")
        out = tmp_path / "d.pptx"
        r = await PptTool().execute(action="create", path=str(out),
                                    title="标题页", bullets=["要点一", "要点二"])
        assert r.success, r.error
        assert out.exists() and out.stat().st_size > 0

        back = await PptTool().execute(action="read", path=str(out))
        assert back.success
        text = "\n".join(s["text"] for s in back.output["slides"])
        assert "标题页" in text and "要点一" in text

    async def test_create_without_any_content_still_errors(self, tmp_path):
        pytest.importorskip("pptx")
        r = await PptTool().execute(action="create", path=str(tmp_path / "e.pptx"))
        assert not r.success
        assert "slides" in (r.error or "")
