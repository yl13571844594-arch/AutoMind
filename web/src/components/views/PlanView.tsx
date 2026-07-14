// 📋 计划视图：最近一次任务的分层执行计划。
import { Card } from 'antd';
import { esc } from '../../lib/markdown';
import { usePanel } from '../../store/panel';

function planHtml(g: any, indent: string): string {
  const icons: any = { pending: '○', in_progress: '◐', completed: '✓', failed: '✗', blocked: '⊘', reverted: '↺' };
  const cls: any = { pending: 'pending', in_progress: 'running', completed: 'done', failed: 'fail', blocked: 'pending', reverted: 'fail' };
  let html = `<div class="node ${cls[g.status] || ''}">${indent}${icons[g.status] || '?'} ${esc(g.description)}`;
  if (g.action) html += ` [${esc(g.action)}]`;
  html += '</div>';
  (g.children || []).forEach((c: any) => { html += planHtml(c, indent + '  '); });
  return html;
}

export default function PlanView() {
  const plan = usePanel((s) => s.plan);
  return (
    <div>
      <h3>📋 最近执行计划</h3>
      <Card size="small" style={{ marginTop: 12 }}>
        {plan?.root_goal ? (
          <div className="plan-tree" dangerouslySetInnerHTML={{ __html: planHtml(plan.root_goal, '') }} />
        ) : (
          <em className="hint-text">暂无计划数据。切换到「工作」模式执行任务后将显示分层计划。</em>
        )}
      </Card>
    </div>
  );
}
