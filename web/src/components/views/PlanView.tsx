// 📋 计划视图：最近一次任务的分层执行计划。
//
// 原实现是把整棵树拼成 HTML 字符串再 dangerouslySetInnerHTML 塞进去，靠
// `white-space: pre-wrap` 保留缩进 —— 能显示，但后端 `_serialize_plan` 送来的
// task / status / execution_order / preconditions / expected_effects /
// revision_history **一个都没用上**，用户只看得到一列灰字。
// 现在改成组件树：顶部给进度概览，节点可展开看前置条件与预期结果，
// 底部列出计划修订历史。
import { Card, Collapse, Empty, Tag, Tooltip } from 'antd';
import { useMemo, useState } from 'react';
import { usePanel } from '../../store/panel';
import { Badge, EmptyState, Tile, ViewHead } from '../ui/Panel';

interface Goal {
  id: string;
  description: string;
  status: string;
  action?: string | null;
  preconditions?: string[];
  expected_effects?: string[];
  children?: Goal[];
}

const STATUS: Record<string, { icon: string; label: string; cls: string }> = {
  pending: { icon: '○', label: '待执行', cls: 'pending' },
  in_progress: { icon: '◐', label: '执行中', cls: 'running' },
  completed: { icon: '✓', label: '已完成', cls: 'done' },
  failed: { icon: '✗', label: '失败', cls: 'fail' },
  blocked: { icon: '⊘', label: '受阻', cls: 'pending' },
  reverted: { icon: '↺', label: '已回滚', cls: 'fail' },
};

const PLAN_STATUS: Record<string, string> = {
  draft: '草稿', executing: '执行中', completed: '已完成',
  failed: '失败', revised: '已修订',
};

/** 深度优先展平，同时记录层级 —— 渲染成缩进列表比嵌套 DOM 更好控溢出。 */
function flatten(g: Goal, depth = 0, out: { g: Goal; depth: number }[] = []) {
  out.push({ g, depth });
  (g.children || []).forEach((c) => flatten(c, depth + 1, out));
  return out;
}

function countByStatus(root: Goal) {
  const acc: Record<string, number> = {};
  for (const { g } of flatten(root)) acc[g.status] = (acc[g.status] || 0) + 1;
  return acc;
}

function GoalRow({ g, depth }: { g: Goal; depth: number }) {
  const [open, setOpen] = useState(false);
  const st = STATUS[g.status] || STATUS.pending;
  const extras = [...(g.preconditions || []), ...(g.expected_effects || [])];
  const hasExtras = extras.length > 0;

  return (
    <div className={`pv-row ${st.cls}`} style={{ paddingLeft: 8 + depth * 20 }}>
      {/* 层级引导线：纯 CSS 竖线，比空格缩进更抗换行 */}
      {depth > 0 && <span className="pv-guide" style={{ left: depth * 20 - 8 }} />}
      <span className="pv-icon" title={st.label}>{st.icon}</span>
      <div className="pv-body">
        <div className="pv-desc">
          <span>{g.description}</span>
          {g.action && <Badge tone="mcp">🛠 {g.action}</Badge>}
          {hasExtras && (
            <button type="button" className="pv-more" onClick={() => setOpen(!open)}>
              {open ? '收起' : '条件/结果'}
            </button>
          )}
        </div>
        {open && (
          <div className="pv-extra">
            {(g.preconditions || []).map((p, i) => (
              <div key={`p${i}`}><span className="pv-extra-k">前置</span> {p}</div>
            ))}
            {(g.expected_effects || []).map((e, i) => (
              <div key={`e${i}`}><span className="pv-extra-k">预期</span> {e}</div>
            ))}
          </div>
        )}
      </div>
      <Tooltip title={st.label}><Tag className="pv-tag">{st.label}</Tag></Tooltip>
    </div>
  );
}

export default function PlanView() {
  const plan = usePanel((s) => s.plan) as any;
  const root: Goal | undefined = plan?.root_goal;
  const rows = useMemo(() => (root ? flatten(root) : []), [root]);
  const counts = useMemo(() => (root ? countByStatus(root) : {}), [root]);

  if (!root) {
    return (
      <div>
        <ViewHead icon="📋" title="执行计划"
                  sub="「工作」模式会先把任务拆成分层目标，再逐个执行。这里显示最近一次的计划树。" />
        <EmptyState icon="📋" title="暂无计划数据"
                    hint={<>切换到「⚙️ 工作」模式执行一个任务后，这里会显示它的分层计划、
                      每一步的状态与前置条件。对话模式不生成计划。</>} />
      </div>
    );
  }

  const total = rows.length;
  const done = counts.completed || 0;
  const failed = (counts.failed || 0) + (counts.reverted || 0);
  const pct = total ? Math.round((done / total) * 100) : 0;

  return (
    <div>
      <ViewHead icon="📋" title="执行计划"
                sub={plan.task || '最近一次任务的分层执行计划'}
                extra={<Tag color={plan.status === 'completed' ? 'green'
                  : plan.status === 'failed' ? 'red' : 'blue'}>
                  {PLAN_STATUS[plan.status] || plan.status}
                </Tag>} />

      <div className="tile-grid">
        <Tile label="🎯 目标总数" value={total} foot="含根目标与子目标" />
        <Tile label="✅ 已完成" value={done} tone={pct === 100 ? 'green' : undefined}
              foot={`完成度 ${pct}%`} />
        <Tile label="◐ 进行中" value={counts.in_progress || 0} foot="正在执行的步骤" />
        <Tile label="✗ 失败 / 回滚" value={failed}
              tone={failed ? 'red' : undefined} foot={failed ? '需要关注' : '无'} />
      </div>

      <Card size="small" styles={{ body: { padding: '6px 4px' } }}>
        <div className="pv-tree">
          {rows.map(({ g, depth }) => <GoalRow key={g.id} g={g} depth={depth} />)}
        </div>
      </Card>

      {(plan.revision_history || []).length > 0 && (
        <Collapse size="small" style={{ marginTop: 12 }} items={[{
          key: 'rev',
          label: `🔁 计划修订历史（${plan.revision_history.length} 次）`,
          children: (
            <div className="feed">
              {plan.revision_history.map((r: any, i: number) => (
                <div key={i} className="feed-row">
                  <span className="feed-time">#{i + 1}</span>
                  <span className="feed-body">
                    {typeof r === 'string' ? r : JSON.stringify(r)}
                  </span>
                </div>
              ))}
            </div>
          ),
        }]} />
      )}

      {!rows.length && <Empty description="计划里没有目标节点" />}
    </div>
  );
}
