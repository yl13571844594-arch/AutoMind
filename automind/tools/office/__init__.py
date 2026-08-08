"""办公自动化工具集 —— Excel / Word / PDF / 邮件。

社区版开放**基础读写**，进阶能力（样式引擎、公式求值、OCR、模板套打、批量
流水线）由 ``office_pro`` 特性门控。分级粒度是"动作"而不是整个工具：
社区版用户能真正把表格读出来、把报告写出去，只是碰不到进阶动作。

第三方依赖一律**懒加载**：这些库不进核心依赖，缺库时返回可照抄的安装命令。
"""

from __future__ import annotations

from automind.tools.office.email_tool import EmailTool
from automind.tools.office.excel_tool import ExcelTool
from automind.tools.office.pdf_tool import PdfTool
from automind.tools.office.word_tool import WordTool

__all__ = ["EmailTool", "ExcelTool", "PdfTool", "WordTool"]
