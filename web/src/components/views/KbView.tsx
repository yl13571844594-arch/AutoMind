// 📚 RAG 知识库：上传文档 / 检索测试 / 自动检索开关。
// 社区版：5 文档 / 10MB / 单库；专业版：无限文档 / 200MB / 多库 / Reranker /
// 引用溯源 / 定时重嵌入 / 外部向量后端；企业版：+ 混合检索 / 热度统计 /
// 检索审计日志 / 目录批量导入 / 总量不限。
import {
  App, Button, Card, Input, Modal, Progress, Select, Space, Switch, Table, Tabs, Tag, Typography, Upload,
} from 'antd';
import { useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';
import { EmptyState, Tile, ViewHead } from '../ui/Panel';

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

  const chunkTotal = docs.reduce((n: number, d: any) => n + (d.chunks || 0), 0);
  const overview = (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <div className="tile-grid" style={{ marginBottom: 0 }}>
        <Tile label="📄 文档数" value={docs.length}
              unit={limits.docs != null ? ` / ${limits.docs}` : undefined}
              tone={limits.docs != null && docs.length >= limits.docs ? 'yellow' : undefined}
              foot={limits.docs != null ? '社区版上限' : '不限'} />
        <Tile label="💾 占用空间" value={fmtSize(sizeUsed)}
              tone={sizePct >= 80 ? 'red' : sizePct >= 50 ? 'yellow' : undefined}
              foot={limits.size != null ? `上限 ${fmtSize(limits.size)}（已用 ${sizePct}%）` : '不限'} />
        <Tile label="🧩 索引片段" value={chunkTotal} foot="可被检索的最小单位" />
        <Tile label="🔎 对话自动检索"
              value={<Switch size="small" checked={data.auto_retrieve}
                onChange={async (v) => {
                  await apiPost('/kb/auto', { enabled: v }); reload();
                  message.info(v ? '对话中将自动检索知识库' : '已关闭自动检索');
                }} />}
              foot={data.auto_retrieve ? '提问时自动带上相关资料' : '当前关闭，仅手动检索'} />
      </div>
      {/* 一条 0% 的空进度条没有任何信息量，没文档时就别占地方 */}
      {limits.size != null && docs.length > 0 && <Progress percent={sizePct} size="small"
        status={sizePct >= 90 ? 'exception' : undefined} />}
      {!pro && (
        <Card size="small">
          <Paragraph type="secondary" style={{ fontSize: '.78em', margin: 0 }}>
            🔒 专业版解锁：无限文档 / 200MB / 多知识库 / Reranker 重排 / 引用溯源 / 定时重嵌入；
            企业版再加混合检索、热度统计、检索审计与批量导入。
          </Paragraph>
        </Card>
      )}

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
        locale={{
          emptyText: <EmptyState icon="📄" title="还没有任何文档"
            hint="上传 PDF / Word / Markdown / TXT 后，对话时 Agent 会自动检索并引用其中的内容。"
            style={{ border: 'none', background: 'transparent' }} />,
        }}
      />

      <Card size="small" title={<span>🔎 检索测试 {tierTag}</span>}>
        <Space.Compact style={{ width: '100%' }}>
          <Input placeholder="输入问题，测试知识库能检索到什么…" value={query}
            onChange={(e) => setQuery(e.target.value)} onPressEnter={search} />
          <Button type="primary" onClick={search}>检索</Button>
        </Space.Compact>
        {results && (
          <div style={{ marginTop: 10 }}>
            {(results.results || []).length === 0 && (
              <EmptyState icon="🔍" title="未检索到相关内容"
                hint="换个说法再试；若知识库刚上传过文档，也可能是这个问题确实没有对应资料。"
                style={{ padding: '22px 16px' }} />
            )}
            {(results.results || []).map((h: any, i: number) => (
              <div key={i} className="kb-hit">
                <div className="kb-hit-head">
                  {pro && <span className="badge badge-mcp">[{i + 1}]</span>}
                  <Text strong>{h.doc_name}</Text>
                  <span className="hint-text">第 {h.seq + 1} 段</span>
                  <span className="kb-score" title={`相关度 ${h.score}`}>
                    <span className="kb-score-bar"
                          style={{ width: `${Math.round(Math.max(0, Math.min(1, h.score)) * 100)}%` }} />
                  </span>
                  <span className="hint-text">{h.score}</span>
                </div>
                <div className="kb-hit-body">{h.text.slice(0, 400)}{h.text.length > 400 ? '…' : ''}</div>
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
      <ViewHead icon="📚" title="RAG 知识库" extra={tierTag}
                sub="上传的资料会被切片并建立向量索引；开启「对话自动检索」后，提问时 Agent 会自动引用相关片段。" />
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
