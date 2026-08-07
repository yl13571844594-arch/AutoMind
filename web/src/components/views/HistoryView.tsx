// 📜 任务历史：搜索 / 筛选 / 分页回溯 · 查看完整产出 · 一键重跑。
//
// 改造前的两个问题：
//   1) 只取默认的最近 50 条 —— 服务端其实存了 200 条（_HISTORY_CAP），
//      剩下 150 条在界面上根本够不着，而界面还写着"已持久化，不会丢"；
//   2) 没有搜索，且把取到的记录一次性全渲染成 antd Card。
// 现在：一次取满 200 条（这个量在内存里做检索毫无压力），关键词 + 模式 + 状态
// 三重筛选，**只渲染当前页**，翻页步进 20。
import { App, Button, Card, Empty, Input, Modal, Pagination, Segmented, Space, Typography } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { apiDelete, apiGet } from '../../api/client';
import { copyText } from '../../lib/clipboard';
import { renderMarkdown } from '../../lib/markdown';
import { MODE_LABELS, useApp, type Mode } from '../../store/app';
import { useChat } from '../../store/chat';

const { Text } = Typography;
const MODE_ICON: Record<string, string> = { chat: '💬', work: '⚙️', coding: '💻', multi: '🤝', loop: '🔁' };
const PAGE_SIZE = 20;
// 与服务端 _HISTORY_CAP 对齐：一次把能取的都取回来，检索/分页在前端做
const FETCH_LIMIT = 200;

export default function HistoryView() {
  const { message, modal } = App.useApp();
  const [history, setHistory] = useState<any[]>([]);
  const [detail, setDetail] = useState<any>(null);
  const [q, setQ] = useState('');
  const [modeFilter, setModeFilter] = useState<string>('all');
  const [okFilter, setOkFilter] = useState<string>('all');
  const [page, setPage] = useState(1);

  const reload = () => apiGet(`/history?limit=${FETCH_LIMIT}`)
    .then((r) => setHistory(Array.isArray(r) ? r : []))
    .catch(() => {});
  useEffect(() => { reload(); }, []);

  // 最新的排前面；筛选与搜索都在这一份上做
  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return history.slice().reverse().filter((h) => {
      if (modeFilter !== 'all' && (h.interaction || '') !== modeFilter) return false;
      if (okFilter === 'ok' && !h.success) return false;
      if (okFilter === 'fail' && h.success) return false;
      if (!kw) return true;
      // 任务正文与产出都参与匹配 —— 用户经常只记得结果里的某个词
      return (h.task || '').toLowerCase().includes(kw)
        || (h.output || '').toLowerCase().includes(kw);
    });
  }, [history, q, modeFilter, okFilter]);

  // 筛选条件变了就回到第一页，否则会停在一个空白的尾页上
  useEffect(() => { setPage(1); }, [q, modeFilter, okFilter]);

  const pageItems = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  // 命中的关键词高亮，扫一眼就知道为什么匹配到这条
  const mark = (s: string) => {
    const kw = q.trim();
    if (!kw) return s;
    const i = s.toLowerCase().indexOf(kw.toLowerCase());
    if (i < 0) return s;
    return (
      <>
        {s.slice(0, i)}
        <mark className="hist-hit">{s.slice(i, i + kw.length)}</mark>
        {s.slice(i + kw.length)}
      </>
    );
  };

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
      const ta = document.querySelector<HTMLTextAreaElement>('.input-inner textarea');
      if (ta) { ta.value = task; ta.focus(); }
    }, 100);
    message.info('任务已填入输入框，确认后按 Enter 发送');
  };

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between' }} align="start">
        <h3 style={{ margin: 0 }}>
          📜 任务历史（{filtered.length}
          {filtered.length !== history.length ? ` / ${history.length}` : ''}）{' '}
          <span className="hint-text" style={{ fontWeight: 400 }}>
            已持久化 — 关浏览器/重启服务都不会丢（最多保留最近 {FETCH_LIMIT} 条）
          </span>
        </h3>
        <Button size="small" danger disabled={!history.length} onClick={() => modal.confirm({
          title: '清空全部任务历史？',
          content: '不可撤销，且会清掉全部记录（不只是当前筛选出的这些）。',
          okText: '清空', okButtonProps: { danger: true }, cancelText: '取消',
          onOk: async () => { await apiDelete('/history'); reload(); message.info('历史已清空'); },
        })}>清空</Button>
      </Space>

      <Space wrap style={{ width: '100%', margin: '10px 0 4px' }}>
        <Input.Search
          allowClear
          placeholder="搜索任务内容或产出…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ width: 300 }}
        />
        <Segmented
          size="small"
          value={modeFilter}
          onChange={(v) => setModeFilter(String(v))}
          options={[
            { label: '全部模式', value: 'all' },
            ...Object.keys(MODE_ICON).map((m) => ({
              label: `${MODE_ICON[m]} ${MODE_LABELS[m as Mode] || m}`, value: m,
            })),
          ]}
        />
        <Segmented
          size="small"
          value={okFilter}
          onChange={(v) => setOkFilter(String(v))}
          options={[
            { label: '全部', value: 'all' },
            { label: '✓ 成功', value: 'ok' },
            { label: '✗ 未完成', value: 'fail' },
          ]}
        />
      </Space>

      {filtered.length === 0 && (
        <Empty
          style={{ marginTop: 40 }}
          description={history.length ? '没有匹配的记录，换个关键词或放宽筛选条件' : '暂无历史记录'}
        />
      )}

      {pageItems.map((h) => (
        <Card key={h.session_id} size="small" style={{ marginTop: 8, borderColor: h.success ? 'var(--green)' : 'var(--red)' }}>
          <Space style={{ width: '100%', justifyContent: 'space-between' }} align="start">
            <div style={{ flex: 1, minWidth: 0 }}>
              <Text strong>{mark((h.task || '').slice(0, 120))}</Text>
              <div className="hint-text" style={{ marginTop: 3 }}>
                {MODE_ICON[h.interaction] || ''}{MODE_LABELS[h.interaction as Mode] || ''}
                {h.scheduled ? ' ⏰' : ''}{h.cached ? ' ⚡缓存' : ''}
                {h.time ? ` · ${h.time}` : ''} · {h.steps}步 · {h.tokens}tk · {h.duration_ms}ms
              </div>
              <div style={{ fontSize: '.82em', color: 'var(--text2)', marginTop: 4, maxHeight: 60, overflow: 'hidden' }}>
                {mark((h.output || '').slice(0, 200))}
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

      {filtered.length > PAGE_SIZE && (
        <Pagination
          style={{ marginTop: 14, textAlign: 'center' }}
          current={page}
          pageSize={PAGE_SIZE}
          total={filtered.length}
          showSizeChanger={false}
          onChange={setPage}
          showTotal={(t, r) => `第 ${r[0]}-${r[1]} 条 / 共 ${t} 条`}
        />
      )}

      <Modal title="🔍 任务详情" open={!!detail} onCancel={() => setDetail(null)} width={720}
        footer={detail && (
          <>
            <Button onClick={async () => {
              const el = document.getElementById('hist-output');
              const ok = await copyText(el?.innerText || '');
              ok ? message.success('已复制') : message.error('复制失败');
            }}>⧉ 复制全部</Button>
            <Button type="primary" onClick={() => { const d = detail; setDetail(null); rerun(d.interaction || 'work', d.task || ''); }}>↻ 重新运行此任务</Button>
          </>
        )}>
        {detail && (
          <>
            <div className="hint-text">
              {MODE_ICON[detail.interaction]}{MODE_LABELS[detail.interaction as Mode]}{detail.time ? ` · ${detail.time}` : ''}
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
