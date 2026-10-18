// 🧭 从执行轨迹生成工作流（v1.10.0，专业版，R-307）：把一次成功的任务整理成 YAML 草稿，
// 打开到编辑器里人审 —— 保存之前它只是草稿，不会落盘、更不会被运行。
import { Alert, App, Button, Empty, List, Modal, Segmented, Space, Tag } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiPost, errText } from '../../api/client';

export default function FromTraceModal({ open, onClose, onDraft }: {
  open: boolean; onClose: () => void;
  onDraft: (yaml: string, suggestedFile: string) => void;
}) {
  const { message } = App.useApp();
  const [rows, setRows] = useState<any[] | null>(null);
  const [locked, setLocked] = useState('');
  const [mode, setMode] = useState<'tools' | 'agent'>('tools');
  const [result, setResult] = useState<any>(null);
  const [busy, setBusy] = useState('');

  const load = useCallback(async () => {
    setResult(null);
    try {
      const d = await apiGet('/workflow/from-trace/candidates?limit=30');
      if (d?.feature) { setLocked(String(d.error || '')); setRows([]); return; }
      setLocked('');
      setRows(d.candidates || []);
    } catch (e) { message.error(errText(e)); setRows([]); }
  }, [message]);
  useEffect(() => { if (open) load(); }, [open, load]);

  const generate = async (r: any) => {
    setBusy(r.run_id);
    try {
      const d = await apiPost('/workflow/from-trace', { session_id: r.session_id, run_id: r.run_id, mode });
      setResult(d);
    } catch (e) { message.error(errText(e), 6); }
    finally { setBusy(''); }
  };

  return (
    <Modal open={open} onCancel={onClose} footer={null} width={720} destroyOnClose
      title="🧭 从执行轨迹生成工作流（草稿）">
      {locked ? <Alert type="info" showIcon message="专业版功能" description={locked} /> : result ? (
        <div>
          <Space style={{ marginBottom: 8 }}>
            <Tag color={result.valid ? 'success' : 'error'}>{result.valid ? '校验通过' : '校验未通过'}</Tag>
            {result.dry_run && <Tag>试运行：{result.dry_run.summary?.failed ? `${result.dry_run.summary.failed} 步失败` : '全部可渲染'}</Tag>}
          </Space>
          <div className="hint-text">保存之前请人工审阅：</div>
          <ul className="hint-text">{(result.notes || []).map((n: string, i: number) => <li key={i}>{n}</li>)}</ul>
          <pre className="mono" style={{ maxHeight: 280, overflow: 'auto', fontSize: '.8em' }}>{result.yaml}</pre>
          <Space>
            <Button onClick={() => setResult(null)}>返回</Button>
            <Button type="primary" onClick={() => { onDraft(result.yaml, result.suggested_file); onClose(); }}>
              在编辑器中打开</Button>
          </Space>
        </div>
      ) : (
        <div>
          <Space style={{ marginBottom: 8 }}>
            <span>生成方式</span>
            <Segmented size="small" value={mode} onChange={(v) => setMode(v as any)} options={[
              { label: '逐步工具（照原路径固化）', value: 'tools' },
              { label: '一个 Agent 步骤（工具白名单）', value: 'agent' }]} />
          </Space>
          {rows === null && <div className="hint-text">正在读取执行轨迹…</div>}
          {rows && !rows.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有可用的执行轨迹：先在「工作」模式下成功完成一个任务" />}
          {rows && rows.length > 0 && (
            <List size="small" dataSource={rows} renderItem={(r: any) => (
              <List.Item actions={[
                <Button key="g" size="small" type="primary" disabled={!r.exportable}
                  loading={busy === r.run_id} onClick={() => generate(r)}>生成草稿</Button>]}>
                <List.Item.Meta
                  title={<span>{r.task?.slice(0, 60) || '（无任务描述）'}
                    {' '}{r.success ? <Tag color="success">成功</Tag> : <Tag color="error">未成功</Tag>}</span>}
                  description={<span className="hint-text">
                    {new Date(r.updated_at * 1000).toLocaleString()} · {r.tool_calls} 次工具调用 · {(r.tools || []).join('、')}
                    {!r.exportable && ' · 只有成功完成的任务才能导出'}</span>} />
              </List.Item>
            )} />
          )}
        </div>
      )}
    </Modal>
  );
}
