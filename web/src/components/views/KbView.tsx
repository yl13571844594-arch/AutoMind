// 📚 RAG 知识库：上传文档 / 检索测试 / 自动检索开关。
// 社区版：5 文档 / 10MB / 单库；专业版：无限文档 / 200MB / 多库 / Reranker /
// 引用溯源 / 定时重嵌入 / 外部向量后端；企业版：+ 混合检索 / 热度统计 /
// 检索审计日志 / 目录批量导入 / 总量不限。
import {
  App, Button, Card, Input, Modal, Progress, Select, Space, Switch, Table, Tabs, Tag, Typography, Upload,
} from 'antd';
import { useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';

const { Text, Paragraph } = Typography;

const fmtSize = (n: number) => n > 1024 * 1024 ? (n / 1024 / 1024).toFixed(1) + ' MB' : (n / 1024).toFixed(1) + ' KB';

export default function KbView() {
  const { message, modal } = App.useApp();
  const [data, setData] = useState<any>(null);
  const [kbSel, setKbSel] = useState('default');
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<any>(null);
  const [uploading, setUploading] = useState(false);
  const [stats, setStats] = useState<any>(null);
  const [log, setLog] = useState<any[]>([]);
  const [importDir, setImportDir] = useState('');

  const reload = () => {
    apiGet('/kb').then((d) => {
      setData(d);
      if (d.enterprise) {
        apiGet('/kb/stats').then(setStats).catch(() => {});
        apiGet('/kb/search-log?limit=50').then((r) => setLog(r.log || [])).catch(() => {});
      }
    }).catch(() => {});
  };
  useEffect(() => { reload(); }, []);

  if (!data) return <Card loading />;

  const { limits, pro, enterprise } = data;
  const docs = data.docs || [];
  const kbs = data.kbs || [];
  const sizeUsed = data.total_size || 0;
  const sizePct = limits.size ? Math.min(100, Math.round((sizeUsed / limits.size) * 100)) : 0;

  const upload = async (file: File) => {
    setUploading(true);
    try {
      const b64: string = await new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res((r.result as string).split(',', 2)[1]);
        r.onerror = rej;
        r.readAsDataURL(file);
      });
      const r = await apiPost('/kb/upload', { name: file.name, content_b64: b64, kb: kbSel });
      if (r.error) message.error(r.error);
      else { message.success(`已入库「${file.name}」（${r.doc.chunks} 个片段）`); reload(); }
    } catch (e: any) {
      message.error('上传失败: ' + e.message);
    }
    setUploading(false);
    return false;
  };

  const search = async () => {
    if (!query.trim()) return;
    const r = await apiPost('/kb/search', { query, top_k: 5, kb: pro && kbSel !== 'default' ? kbSel : undefined });
    if (r.error) { message.error(r.error); return; }
    setResults(r);
    if (enterprise) reload();
  };

  const tierTag = enterprise ? <Tag color="purple">企业版 · 混合检索</Tag>
    : pro ? <Tag color="blue">专业版 · Reranker</Tag> : <Tag>社区版</Tag>;

  const overview = (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <Card size="small">
        <Space wrap size="large">
          <span>📄 文档 <b>{docs.length}</b>{limits.docs != null && ` / ${limits.docs}`}</span>
          <span>💾 总量 <b>{fmtSize(sizeUsed)}</b>{limits.size != null && ` / ${fmtSize(limits.size)}`}</span>
          {limits.size != null && <Progress percent={sizePct} size="small" style={{ width: 160 }} />}
          <span>自动检索
            <Switch size="small" style={{ marginLeft: 8 }} checked={data.auto_retrieve}
              onChange={async (v) => { await apiPost('/kb/auto', { enabled: v }); reload(); message.info(v ? '对话中将自动检索知识库' : '已关闭自动检索'); }} />
          </span>
        </Space>
        {!pro && (
          <Paragraph type="secondary" style={{ fontSize: '.78em', margin: '8px 0 0' }}>
            🔒 专业版解锁：无限文档 / 200MB / 多知识库 / Reranker 重排 / 引用溯源 / 定时重嵌入；企业版再加混合检索、热度统计、检索审计与批量导入。
          </Paragraph>
        )}
      </Card>

      <Space wrap>
        {pro && (
          <Select value={kbSel} onChange={setKbSel} style={{ width: 200 }}
            options={kbs.map((k: any) => ({ value: k.id, label: `${k.name}（${k.docs} 文档）` }))} />
        )}
        <Upload showUploadList={false} accept=".pdf,.docx,.md,.markdown,.txt" beforeUpload={upload as any} multiple>
          <Button type="primary" loading={uploading}>⬆ 上传文档（PDF / Word / MD / TXT）</Button>
        </Upload>
        {pro && (
          <>
            <Button onClick={() => {
              let name = '';
              modal.confirm({
                title: '新建知识库',
                content: <Input placeholder="知识库名称（按主题分类）" onChange={(e) => { name = e.target.value; }} />,
                onOk: async () => {
                  if (!name.trim()) return;
                  const r = await apiPost('/kb/kbs', { name });
                  if (r.error) message.error(r.error);
                  else { message.success('知识库已创建'); reload(); }
                },
              });
            }}>➕ 新建知识库</Button>
            {kbSel !== 'default' && (
              <Button danger onClick={() => modal.confirm({
                title: '删除该知识库及其全部文档？',
                onOk: async () => {
                  const r = await apiDelete(`/kb/kbs/${kbSel}`);
                  if (r.error) message.error(r.error);
                  else { message.info('已删除'); setKbSel('default'); reload(); }
                },
              })}>删除该库</Button>
            )}
            <Button onClick={async () => {
              const r = await apiPost('/kb/reembed');
              if (r.error) message.error(r.error);
              else message.success(`已重新嵌入 ${r.chunks} 个片段`);
            }}>♻ 重新嵌入</Button>
          </>
        )}
        {enterprise && (
          <Space.Compact>
            <Input style={{ width: 260 }} placeholder="服务器目录路径（批量导入）" value={importDir}
              onChange={(e) => setImportDir(e.target.value)} />
            <Button onClick={async () => {
              if (!importDir.trim()) { message.error('请输入目录路径'); return; }
              const r = await apiPost('/kb/import-dir', { directory: importDir.trim(), kb: kbSel });
              if (r.error) { message.error(r.error); return; }
              message.success(`批量导入完成：${r.imported} 个文档${r.skipped ? `，跳过 ${r.skipped}` : ''}`);
              reload();
            }}>📥 批量导入</Button>
          </Space.Compact>
        )}
      </Space>

      <Table
        size="small" rowKey="id" pagination={docs.length > 10 ? { pageSize: 10 } : false}
        dataSource={pro ? docs.filter((d: any) => d.kb === kbSel) : docs}
        columns={[
          { title: '文档', dataIndex: 'name', ellipsis: true },
          { title: '大小', dataIndex: 'size', width: 90, render: fmtSize },
          { title: '片段', dataIndex: 'chunks', width: 70 },
          ...(enterprise && stats ? [{
            title: '🔥 命中', width: 80,
            render: (_: any, r: any) => (stats.docs || []).find((s: any) => s.doc_id === r.id)?.hits || 0,
          }] : []),
          { title: '入库时间', dataIndex: 'time', width: 150 },
          {
            title: '', width: 60,
            render: (_: any, r: any) => (
              <Button size="small" danger type="text" onClick={() => modal.confirm({
                title: `删除文档「${r.name}」？`,
                onOk: async () => { await apiDelete(`/kb/doc/${r.id}`); message.info('已删除'); reload(); },
              })}>🗑</Button>
            ),
          },
        ]}
        locale={{ emptyText: '还没有文档 — 上传 PDF / Word / Markdown / TXT，对话时 Agent 会自动检索引用' }}
      />

      <Card size="small" title={<span>🔎 检索测试 {tierTag}</span>}>
        <Space.Compact style={{ width: '100%' }}>
          <Input placeholder="输入问题，测试知识库能检索到什么…" value={query}
            onChange={(e) => setQuery(e.target.value)} onPressEnter={search} />
          <Button type="primary" onClick={search}>检索</Button>
        </Space.Compact>
        {results && (
          <div style={{ marginTop: 10 }}>
            {(results.results || []).length === 0 && <em className="hint-text">未检索到相关内容</em>}
            {(results.results || []).map((h: any, i: number) => (
              <div key={i} style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 10, marginTop: 8, fontSize: '.86em' }}>
                <Space size="small">
                  {pro && <Tag color="blue">[{i + 1}]</Tag>}
                  <Text strong>{h.doc_name}</Text>
                  <span className="hint-text">第 {h.seq + 1} 段 · 相关度 {h.score}</span>
                </Space>
                <div style={{ color: 'var(--text2)', marginTop: 4, whiteSpace: 'pre-wrap' }}>{h.text.slice(0, 400)}{h.text.length > 400 ? '…' : ''}</div>
              </div>
            ))}
            <div className="hint-text" style={{ marginTop: 8 }}>
              {results.hybrid ? '✓ 企业版混合检索（向量语义 + 词法精确双通道）' : results.reranked ? '✓ 专业版 Reranker 已启用' : '社区版向量检索'}
              {pro && ' · 对话中引用将标注 [编号] 与来源（引用溯源）'}
            </div>
          </div>
        )}
      </Card>

      {pro && <ProSettings data={data} onChange={reload} />}
    </Space>
  );

  const items = [{ key: 'docs', label: '📄 文档与检索', children: overview }];
  if (enterprise) {
    items.push({
      key: 'stats', label: '🔥 热度统计', children: (
        <Card size="small">
          <Paragraph type="secondary" style={{ fontSize: '.82em' }}>
            总检索次数：<b>{stats?.search_count || 0}</b> — 哪些文档最常被命中，帮助识别高价值资料与该淘汰的死文档。
          </Paragraph>
          <Table size="small" rowKey="doc_id" pagination={false}
            dataSource={stats?.docs || []}
            columns={[
              { title: '文档', dataIndex: 'doc_name' },
              { title: '命中次数', dataIndex: 'hits', width: 120, sorter: (a: any, b: any) => a.hits - b.hits, defaultSortOrder: 'descend' as const },
            ]} />
        </Card>
      ),
    });
    items.push({
      key: 'log', label: '🛡 检索审计', children: (
        <Card size="small">
          <Paragraph type="secondary" style={{ fontSize: '.82em' }}>
            记录每次知识库检索的查询与命中来源（含对话自动检索），满足合规审计需要。
          </Paragraph>
          {log.length === 0 ? <em className="hint-text">暂无检索记录</em> : log.map((e, i) => (
            <div key={i} style={{ borderBottom: '1px dashed var(--border)', padding: '6px 0', fontSize: '.82em' }}>
              <span className="mono hint-text">{e.time}</span>
              <Tag style={{ marginLeft: 6, fontSize: '.7em' }}>{e.source === 'chat' ? '对话自动' : 'API'}</Tag>
              <b> {e.query}</b>
              <div className="hint-text" style={{ marginTop: 2 }}>
                命中：{(e.hits || []).map((h: any) => `${h.doc}·第${h.seq + 1}段(${h.score})`).join('、') || '（无）'}
              </div>
            </div>
          ))}
        </Card>
      ),
    });
  }

  return (
    <div>
      <h3 style={{ marginBottom: 12 }}>📚 RAG 知识库 {tierTag}</h3>
      {items.length > 1 ? <Tabs items={items} /> : overview}
    </div>
  );
}

function ProSettings({ data, onChange }: { data: any; onChange: () => void }) {
  const { message } = App.useApp();
  const [backend, setBackend] = useState(data.settings?.backend || 'builtin');
  const [hours, setHours] = useState(String(data.settings?.auto_reembed_hours || 0));
  return (
    <Card size="small" title="⚙ 专业版设置">
      <Space wrap size="large">
        <span>
          向量后端：
          <Select size="small" value={backend} onChange={setBackend} style={{ width: 140, marginLeft: 6 }}
            options={[
              { value: 'builtin', label: '内置（离线）' }, { value: 'chromadb', label: 'ChromaDB' },
              { value: 'milvus', label: 'Milvus' }, { value: 'pinecone', label: 'Pinecone' },
              { value: 'qdrant', label: 'Qdrant' },
            ]} />
        </span>
        <span>
          定时重嵌入（小时，0=关闭）：
          <Input size="small" style={{ width: 80, marginLeft: 6 }} value={hours} onChange={(e) => setHours(e.target.value)} />
        </span>
        <Button size="small" type="primary" onClick={async () => {
          const r = await apiPost('/kb/settings', { backend, auto_reembed_hours: parseFloat(hours) || 0 });
          if (r.error) { message.error(r.error); return; }
          message.success('设置已保存');
          onChange();
        }}>保存设置</Button>
      </Space>
      <div className="hint-text" style={{ marginTop: 6 }}>
        外部向量后端需先安装对应 SDK（如 pip install qdrant-client）；未配置时使用内置向量存储（离线可用）。
        {data.settings?.last_reembed && ` · 上次重嵌入：${data.settings.last_reembed}`}
      </div>
    </Card>
  );
}
