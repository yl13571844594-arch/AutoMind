"""专家系统 — 专家 = 可复用的角色设定（人设 + 专业提示词）。

激活某个专家后，任务会带上该专家的角色设定执行（全部交互模式生效）。

分层能力：
    社区版（免费）：浏览官方精选 10 个专家、一键安装、自建专家最多 3 个；
    专业版（experts_pro）：自建数量不限、团队分享（shared 标记）、
        JSON 导入/导出、使用统计展示；
    企业版（expert_approval）：企业专家市场 —— 分享的专家需管理员审批
        （approved）后其他成员才可见可用。

存储：``.automind/experts.json``（服务器级 → 同一部署天然团队共享）。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

#: 社区版自建专家上限（experts_pro 特性解除）
COMMUNITY_MAX_CUSTOM = 3

#: 官方精选专家（社区版可浏览并一键安装）
OFFICIAL_EXPERTS: list[dict] = [
    {"id": "fe-master", "icon": "🎨", "name": "前端专家",
     "desc": "HTML/CSS/JS/React/Vue，注重可访问性与性能",
     "prompt": "你是资深前端工程师，精通 HTML/CSS/JavaScript/TypeScript 与 React/Vue 生态。"
               "输出可直接运行的代码，注重语义化、可访问性、响应式与性能；样式遵循现代最佳实践。"},
    {"id": "be-architect", "icon": "🏗️", "name": "后端架构师",
     "desc": "API 设计、数据库、微服务与性能调优",
     "prompt": "你是后端架构师，擅长 API 设计（REST/GraphQL）、数据库建模、缓存、消息队列与微服务拆分。"
               "方案先讲清取舍再落地代码，默认考虑幂等、并发与可观测性。"},
    {"id": "data-analyst", "icon": "📊", "name": "数据分析师",
     "desc": "pandas/SQL/可视化，从数据到结论",
     "prompt": "你是数据分析师，精通 pandas、SQL 与可视化。拿到数据先看质量与分布，"
               "分析要给出方法、代码与明确结论，图表标题与坐标轴必须可读。"},
    {"id": "crawler", "icon": "🕷️", "name": "爬虫专家",
     "desc": "requests/解析/反爬对策，合规采集",
     "prompt": "你是网页采集专家，精通 requests/httpx、HTML 解析与常见反爬对策。"
               "默认加入请求间隔、重试与 UA 设置，提醒用户遵守目标站点的 robots 与法律边界。"},
    {"id": "qa-engineer", "icon": "🧪", "name": "测试工程师",
     "desc": "pytest/单元/集成测试，边界与异常优先",
     "prompt": "你是测试工程师，擅长 pytest 与测试设计。为代码补测试时优先覆盖边界、异常与回归场景，"
               "测试命名清晰表意，必要时使用 fixture/mock，写完运行确认通过。"},
    {"id": "devops", "icon": "🚢", "name": "DevOps 工程师",
     "desc": "Docker/CI/部署脚本/运维排查",
     "prompt": "你是 DevOps 工程师，精通 Docker、CI/CD、Shell/PowerShell 与生产部署。"
               "配置最小可用且安全（非 root、健康检查、资源限制），排障先看日志与指标。"},
    {"id": "copywriter", "icon": "✍️", "name": "文案写手",
     "desc": "标题、营销文案、公众号/小红书风格",
     "prompt": "你是资深中文文案写手，擅长标题打磨与不同平台风格（公众号/小红书/知乎）。"
               "文案要有钩子、有结构、口语流畅，避免 AI 腔与空话。"},
    {"id": "translator", "icon": "🌍", "name": "专业译者",
     "desc": "中英互译，信达雅 + 术语准确",
     "prompt": "你是专业译者，中英互译信达雅：忠实原意、术语准确、行文地道。"
               "技术内容保留专业术语原文对照，文学内容注重语感与节奏。"},
    {"id": "pm", "icon": "📋", "name": "产品经理",
     "desc": "需求拆解、PRD、用户故事与优先级",
     "prompt": "你是产品经理，擅长把模糊想法拆解为清晰需求：用户故事、验收标准、优先级（MoSCoW）。"
               "输出结构化 PRD 片段，主动指出遗漏的边界场景与风险。"},
    {"id": "sec-auditor", "icon": "🛡️", "name": "安全审计员",
     "desc": "代码安全审查：注入/越权/密钥泄漏",
     "prompt": "你是应用安全审计员，审查代码中的注入、越权、XSS/CSRF、密钥泄漏与依赖风险。"
               "发现问题按严重度分级，给出可直接落地的修复代码。"},
]


class ExpertStore:
    """专家存取与业务规则（安装/创建/激活/用量/上限）。"""

    def __init__(self, store_path: str | Path | None = None) -> None:
        self._path = Path(store_path or Path(".automind") / "experts.json")

    # ── 存储 ──
    def _load(self) -> list[dict]:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    def _save(self, items: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    # ── 查询 ──
    def installed(self) -> list[dict]:
        return self._load()

    def get(self, eid: str) -> dict | None:
        for e in self._load():
            if e.get("id") == eid:
                return e
        return None

    def official_catalog(self) -> list[dict]:
        installed_ids = {e["id"] for e in self._load()}
        return [{**e, "installed": e["id"] in installed_ids}
                for e in OFFICIAL_EXPERTS]

    def custom_count(self, owner: str | None = None) -> int:
        return sum(1 for e in self._load()
                   if e.get("source") == "custom"
                   and (owner is None or e.get("owner") == owner))

    # ── 变更 ──
    def install_official(self, eid: str) -> tuple[dict | None, str]:
        cat = {e["id"]: e for e in OFFICIAL_EXPERTS}
        if eid not in cat:
            return None, f"官方专家不存在: {eid}"
        items = self._load()
        if any(e.get("id") == eid for e in items):
            return None, "该专家已安装"
        expert = {**cat[eid], "source": "official", "owner": "official",
                  "shared": True, "approved": True, "usage": 0,
                  "created": time.strftime("%Y-%m-%d")}
        items.append(expert)
        self._save(items)
        return expert, ""

    def create(self, data: dict, owner: str, unlimited: bool,
               needs_approval: bool = False) -> tuple[dict | None, str]:
        name = (data.get("name") or "").strip()[:24]
        prompt = (data.get("prompt") or "").strip()[:4000]
        if not name or not prompt:
            return None, "name 与 prompt 必填"
        if not unlimited and self.custom_count() >= COMMUNITY_MAX_CUSTOM:
            return None, (f"社区版最多创建 {COMMUNITY_MAX_CUSTOM} 个专家 —— "
                          f"专业版可无限创建（experts_pro）")
        items = self._load()
        eid = (data.get("id") or "").strip() or "x_" + uuid.uuid4().hex[:8]
        expert = {
            "id": eid, "name": name,
            "icon": (data.get("icon") or "🎓").strip()[:4],
            "desc": (data.get("desc") or "").strip()[:80],
            "prompt": prompt, "source": "custom", "owner": owner,
            "shared": bool(data.get("shared", False)),
            "approved": not needs_approval,  # 企业审批流开启时需管理员批准
            "usage": 0, "created": time.strftime("%Y-%m-%d"),
        }
        items = [e for e in items if e.get("id") != eid]
        items.append(expert)
        self._save(items)
        return expert, ""

    def delete(self, eid: str) -> bool:
        items = self._load()
        kept = [e for e in items if e.get("id") != eid]
        if len(kept) == len(items):
            return False
        self._save(kept)
        return True

    def update(self, eid: str, patch: dict) -> dict | None:
        items = self._load()
        for e in items:
            if e.get("id") == eid:
                for k in ("name", "icon", "desc", "prompt", "shared", "approved"):
                    if k in patch:
                        e[k] = patch[k]
                self._save(items)
                return e
        return None

    def bump_usage(self, eid: str) -> None:
        items = self._load()
        for e in items:
            if e.get("id") == eid:
                e["usage"] = int(e.get("usage", 0)) + 1
                self._save(items)
                return


#: 进程级单例（Web 层共享；测试可自行构造 ExpertStore）
STORE = ExpertStore()
