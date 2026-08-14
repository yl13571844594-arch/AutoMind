// 面板通用构件：页头 / 实体卡 / 来源徽标 / 统计块 / 空态。
//
// 各视图此前都在自己文件里写内联样式，同一个"卡片"在工具面板、知识库、
// 专家市场里的边距圆角字号全不一样，加一个面板就再抄一遍。这里把重复的
// 部分收成组件，样式落在 global.css 的 .ent-* / .tile / .empty-state 上。
import type { CSSProperties, ReactNode } from 'react';

/** 能力来源 —— 用户需要一眼看出哪些是开箱即用、哪些是自己接进来的。 */
export type Source = 'builtin' | 'mcp' | 'plugin' | 'custom' | 'markdown' | 'python';

const SOURCE_META: Record<Source, { label: string; cls: string; icon: string }> = {
  builtin: { label: '内置', cls: 'badge-builtin', icon: '📦' },
  mcp: { label: 'MCP', cls: 'badge-mcp', icon: '🔌' },
  plugin: { label: '插件', cls: 'badge-plugin', icon: '🧩' },
  custom: { label: '自定义', cls: 'badge-custom', icon: '✎' },
  markdown: { label: 'SKILL.md', cls: 'badge-plugin', icon: '📝' },
  python: { label: 'Python', cls: 'badge-custom', icon: '🐍' },
};

/** 来源徽标（内置 / MCP / 插件 / 自定义…）。 */
export function SourceBadge({ source }: { source?: string }) {
  const meta = SOURCE_META[(source || 'builtin') as Source];
  if (!meta) return null;
  return <span className={`badge ${meta.cls}`}>{meta.icon} {meta.label}</span>;
}

/** 通用徽标。 */
export function Badge({ tone = 'muted', children }: { tone?: string; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

/** 视图页头：图标 + 标题 + 一句话说明 + 右侧操作。 */
export function ViewHead(
  { icon, title, sub, extra }: { icon: string; title: string; sub?: ReactNode; extra?: ReactNode },
) {
  return (
    <div className="view-head">
      <h3>
        <span className="vh-icon">{icon}</span>
        <span>{title}</span>
        {extra && <span className="vh-right">{extra}</span>}
      </h3>
      {sub && <div className="vh-sub">{sub}</div>}
    </div>
  );
}

/** 分组小标题（带计数）。 */
export function SectionTitle({ children, count }: { children: ReactNode; count?: number }) {
  return (
    <div className="sec-title">
      <span>{children}</span>
      {count !== undefined && <span className="sec-count">{count}</span>}
    </div>
  );
}

/**
 * 实体卡（工具 / 技能 / MCP / 插件 / 专家 / 文档）。
 *
 * `body` 列带 min-width:0 —— 长描述、长参数名会在卡内换行，而不是把图标
 * 挤出容器边界（工具面板图标溢出就是这么来的）。
 */
export function EntityCard(
  { icon, title, badges, desc, meta, actions, state, onClick }: {
    icon: ReactNode;
    title: ReactNode;
    badges?: ReactNode;
    desc?: ReactNode;
    meta?: ReactNode;
    actions?: ReactNode;
    state?: 'off' | 'ok' | 'bad';
    onClick?: () => void;
  },
) {
  return (
    <div className={`ent-card${state ? ' ' + state : ''}`}
         onClick={onClick} style={onClick ? { cursor: 'pointer' } : undefined}>
      <div className="ent-ico">{icon}</div>
      <div className="ent-body">
        <div className="ent-title">
          <span className="ent-name">{title}</span>
          {badges}
        </div>
        {desc && <div className="ent-desc">{desc}</div>}
        {meta && <div className="ent-meta">{meta}</div>}
      </div>
      {actions && <div className="ent-act" onClick={(e) => e.stopPropagation()}>{actions}</div>}
    </div>
  );
}

/** 统计块。value 用等宽数字，数值跳动时不会左右晃。 */
export function Tile(
  { label, value, unit, foot, tone }: {
    label: ReactNode; value: ReactNode; unit?: string; foot?: ReactNode;
    tone?: 'green' | 'yellow' | 'red';
  },
) {
  return (
    <div className={`tile${tone ? ' ' + tone : ''}`}>
      <div className="tile-label">{label}</div>
      <div className="tile-value">{value}{unit && <small>{unit}</small>}</div>
      {foot && <div className="tile-foot">{foot}</div>}
    </div>
  );
}

/** 空态：只说"暂无数据"没用，得说清为什么空、下一步做什么。 */
export function EmptyState(
  { icon = '📭', title, hint, style }: {
    icon?: string; title: string; hint?: ReactNode; style?: CSSProperties;
  },
) {
  return (
    <div className="empty-state" style={style}>
      <div className="es-ico">{icon}</div>
      <div className="es-title">{title}</div>
      {hint && <div className="es-hint">{hint}</div>}
    </div>
  );
}
