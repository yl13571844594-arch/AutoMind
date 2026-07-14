// 📜 任务历史：持久化回溯 / 查看完整产出 / 一键重跑。
import { App, Button, Card, Modal, Space, Tag, Typography } from 'antd';
import { useEffect, useState } from 'react';
import { apiDelete, apiGet } from '../../api/client';
import { renderMarkdown } from '../../lib/markdown';
import { MODE_LABELS, useApp, type Mode } from '../../store/app';
import { useChat } from '../../store/chat';

const { Text } = Typography;
const MODE_ICON: Record<string, string> = { chat: '💬', work: '⚙️', coding: '💻', multi: '🤝', loop: '🔁' };

export default function HistoryView() {
  const { message, modal } = App.useApp();
  const [history, setHistory] = useState<any[]>([]);
  const [detail, setDetail] = useState<any>(null);

  const reload = () => apiGet('/history').then((r) => setHistory(Array.isArray(r) ? r : [])).catch(() => {});
  useEffect(() => { reload(); }, []);

  const rerun = async (mode: string, task: string) => {
    if (!task) { message.error('该记录没有任务内容'); return; }
    if (useApp.getState().running) { message.error('有任务正在执行，请先停止'); return; }
    if (['chat', 'work', 'coding', 'multi', 'loop'].includes(mode) && mode !== useApp.getState().mode) {
      await useApp.getState().setMode(mode as Mode);
    } else {
      useApp.getState().setView('chat');
    }
    useChat.getState().setInputDraft(task);
    setTimeout(() => {
      const ta = document.querySelector<HTMLTextAreaElement>('textarea');
      if (ta) { ta.value = task; ta.focus(); }
    }, 100);
    message.info('任务已填入输入框，确认后按 Enter 发送');
  };

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between' }}>
        <h3>📜 任务历史 ({history.length}) <span className="hint-text" style={{ fontWeight: 400 }}>已持久化 — 关浏览器/重启服务都不会丢</span></h3>
        <Button size="small" danger onClick={() => modal.confirm({
          title: '清空全部任务历史？',
          onOk: async () => { await apiDelete('/history'); reload(); message.info('历史已清空'); },
        })}>清空</Button>
      </Space>
      {history.length === 0 && <em className="hint-text">暂无历史记录</em>}
      {history.slice().reverse().map((h) => (
        <Card key={h.session_id} size="small" style={{ marginTop: 8, borderColor: h.success ? 'var(--green)' : 'var(--red)' }}>
          <Space style={{ width: '100%', justifyContent: 'space-between' }} align="start">
            <div style={{ flex: 1, minWidth: 0 }}>
              <Text strong>{(h.task || '').slice(0, 120)}</Text>
              <div className="hint-text" style={{ marginTop: 3 }}>
                {MODE_ICON[h.interaction] || ''}{MODE_LABELS[h.interaction] || ''}{h.scheduled ? ' ⏰' : ''}{h.cached ? ' ⚡缓存' : ''}
                {h.time ? ` · ${h.time}` : ''} · {h.steps}步 · {h.tokens}tk · {h.duration_ms}ms
              </div>
              <div style={{ fontSize: '.82em', color: 'var(--text2)', marginTop: 4, maxHeight: 60, overflow: 'hidden' }}>
                {(h.output || '').slice(0, 200)}
              </div>
            </div>
            <Space direction="vertical" size={4}>
              <Button size="small" onClick={async () => {
                const d = await apiGet(`/history/${encodeURIComponent(h.session_id)}`);
                if (d.error) { message.error(d.error); return; }
                setDetail(d);
              }}>🔍 查看</Button>
              <Button size="small" onClick={() => rerun(h.interaction || 'work', h.task || '')}>↻ 重跑</Button>
              <Button size="small" danger onClick={async () => {
                await apiDelete(`/history/${h.session_id}`); reload(); message.info('记录已删除');
              }}>删除</Button>
            </Space>
          </Space>
        </Card>
      ))}

      <Modal title="🔍 任务详情" open={!!detail} onCancel={() => setDetail(null)} width={720}
        footer={detail && (
          <>
            <Button onClick={() => {
              const el = document.getElementById('hist-output');
              if (el) navigator.clipboard?.writeText(el.innerText).then(() => message.success('已复制'));
            }}>⧉ 复制全部</Button>
            <Button type="primary" onClick={() => { const d = detail; setDetail(null); rerun(d.interaction || 'work', d.task || ''); }}>↻ 重新运行此任务</Button>
          </>
        )}>
        {detail && (
          <>
            <div className="hint-text">
              {MODE_ICON[detail.interaction]}{MODE_LABELS[detail.interaction]}{detail.time ? ` · ${detail.time}` : ''}
              · {detail.steps || 0}步 · {detail.tokens || 0}tk · {detail.duration_ms || 0}ms
              · {detail.success ? <span style={{ color: 'var(--green)' }}>成功</span> : <span style={{ color: 'var(--red)' }}>未完成</span>}
            </div>
            <Text strong style={{ display: 'block', marginTop: 10 }}>任务</Text>
            <Card size="small" style={{ maxHeight: 120, overflowY: 'auto', whiteSpace: 'pre-wrap', fontSize: '.88em' }}>{detail.task || ''}</Card>
            <Text strong style={{ display: 'block', marginTop: 10 }}>完整产出（可复制回收好代码）</Text>
            <Card size="small" style={{ maxHeight: 340, overflowY: 'auto', fontSize: '.86em' }}>
              <div id="hist-output" dangerouslySetInnerHTML={{ __html: renderMarkdown(detail.output || '（无输出）') }} />
            </Card>
          </>
        )}
      </Modal>
    </div>
  );
}
