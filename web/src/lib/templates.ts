// 内置 10 个基础任务模板（与经典版一致）
export interface Template { icon: string; title: string; mode: 'chat' | 'work' | 'coding'; desc: string; prompt: string }

export const TEMPLATES: Template[] = [
  { icon: '🌐', title: '个人主页', mode: 'coding', desc: '生成一个漂亮的响应式个人主页',
    prompt: '帮我生成一个响应式的个人主页 index.html：深色科技风，包含姓名标语、关于我、技能列表、项目卡片和联系方式区块，CSS 内联在文件里，不依赖外部库。' },
  { icon: '📦', title: '项目脚手架', mode: 'work', desc: '创建一个规范的 Python 项目骨架',
    prompt: '在当前目录创建一个 FastAPI 项目骨架：包含 app/main.py（含 /health 健康检查）、requirements.txt、README.md、tests/ 目录和一个示例测试，创建后运行测试验证。' },
  { icon: '🐍', title: '实用脚本', mode: 'coding', desc: '写一个整理文件的 Python 脚本',
    prompt: '写一个 Python 脚本 organize.py：把指定目录下的文件按扩展名分类移动到 images/、docs/、videos/、others/ 子目录，带 --dry-run 预览模式和执行日志。' },
  { icon: '🔧', title: '修复报错', mode: 'coding', desc: '粘贴报错信息，自动定位并修复',
    prompt: '我的代码报错了，请阅读相关文件、定位原因并修复，修复后运行验证。报错信息如下：\n（把报错粘贴到这里）' },
  { icon: '🧪', title: '补单元测试', mode: 'coding', desc: '为现有代码生成 pytest 测试',
    prompt: '阅读项目中的核心模块，为主要函数补充 pytest 单元测试（含边界与异常用例），放到 tests/ 目录，写完运行确认全部通过。' },
  { icon: '📄', title: '生成 README', mode: 'coding', desc: '扫描项目自动写文档',
    prompt: '阅读当前项目的代码结构与入口文件，生成一份专业的 README.md：项目简介、功能列表、安装步骤、使用示例和目录结构说明。' },
  { icon: '📊', title: '数据分析', mode: 'work', desc: '分析数据文件并产出报告',
    prompt: '分析当前目录下的数据文件（CSV/Excel），统计关键指标与分布，生成一份带结论的分析报告 report.html（内嵌图表，可直接在浏览器打开）。' },
  { icon: '🕷️', title: '网页抓取', mode: 'coding', desc: '写爬虫抓取网页数据',
    prompt: '写一个 Python 爬虫脚本：抓取指定网页的标题和正文要点，保存为 JSON 文件，带请求间隔与异常重试。目标网址：\n（把网址粘贴到这里）' },
  { icon: '📝', title: '写周报', mode: 'chat', desc: '把要点整理成一篇周报',
    prompt: '把下面的工作要点整理成一篇结构清晰的周报（本周完成 / 数据与结果 / 问题与风险 / 下周计划）：\n（把要点粘贴到这里）' },
  { icon: '🌍', title: '翻译润色', mode: 'chat', desc: '中英互译并润色表达',
    prompt: '把下面的内容翻译成地道的英文（保留专业术语准确性），并在末尾附一版更精炼的表达建议：\n（把内容粘贴到这里）' },
];
