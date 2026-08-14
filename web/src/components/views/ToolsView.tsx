// 🔧 工具面板：工具 / 技能 / MCP / 插件 四个分栏。
import { App, Button, Card, Input, Segmented, Space, Switch, Tabs, Typography, Upload } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';
import { SKILL_META, TOOL_META, toolDesc, toolLabel } from '../../lib/toolMeta';
import { Badge, EmptyState, EntityCard, SectionTitle, SourceBadge, Tile, ViewHead } from '../ui/Panel';

const { Text, Paragraph } = Typography;
const TOOL_ICONS: Record<string, string> = {
  terminal: '⌨️', file_read: '📖', file_write: '✍️', file_edit: '✏️',
  file_search: '🔎', python_sandbox: '🐍', browser: '🌐', web_fetch: '🔗',
  web_search: '🔍', http_request: '📡', code_generate: '🧬', archive: '🗜️',
  excel_tool: '📊', word_tool: '📄', pdf_tool: '📕', ppt_tool: '📽️',
  email_tool: '✉️', calendar: '📅', db_query: '🗄️', csv_tool: '🧾',
  screenshot_tool: '🖼️', ocr_tool: '🔤', image_tool: '🎨', chart_tool: '📈',
  audio_tool: '🎵', video_tool: '🎬', git_tool: '🌿', process_tool: '⚙️',
  clipboard_tool: '📋', notify: '🔔', im_integration: '💬',
};
const TIER_LABEL: Record<string, string> = { safe: '安全', sensitive: '敏感', dangerous: '危险' };

function ToolsTab() {
  const { message } = App.useApp();
  const [tools, setTools] = useState<any[]>([]);
  const [kw, setKw] = useState('');
  const [filter, setFilter] = useState('全部');
  const reload = () => apiGet('/tools').then(setTools).catch(() => {});
  useEffect(() => { reload(); }, []);

  const counts = useMemo(() => ({
    builtin: tools.filter((t) => t.source === 'builtin').length,
    mcp: tools.filter((t) => t.source === 'mcp').length,
    plugin: tools.filter((t) => t.source === 'plugin').length,
  }), [tools]);

  const shown = useMemo(() => {
    const q = kw.trim().toLowerCase();
    return tools.filter((t) => {
      if (filter === '内置' && t.source !== 'builtin') return false;
      if (filter === 'MCP' && t.source !== 'mcp') return false;
      if (filter === '插件' && t.source !== 'plugin') return false;
      if (filter === '已禁用' && t.enabled) return false;
      if (!q) return true;
      return t.name.toLowerCase().includes(q)
        || toolLabel(t.name).toLowerCase().includes(q)
        || toolDesc(t.name, t.description || '').toLowerCase().includes(q)
        || (t.description || '').toLowerCase().includes(q);
    });
  }, [tools, kw, filter]);

  return (
    <>
      <div className="tile-grid">
        <Tile label="🛠 工具总数" value={tools.length}
              foot={`${tools.filter((t) => t.enabled).length} 个已启用`} />
        <Tile label="📦 内置" value={counts.builtin} tone="green" foot="随包分发，开箱即用" />
        <Tile label="🔌 MCP 接入" value={counts.mcp} foot="来自已连接的 MCP 服务器" />
        <Tile label="🧩 插件提供" value={counts.plugin} foot="由已加载的插件注册" />
      </div>

      <Space style={{ width: '100%', marginBottom: 12 }} wrap>
        <Segmented value={filter} onChange={(v) => setFilter(String(v))}
                   options={['全部', '内置', 'MCP', '插件', '已禁用']} />
        <Input.Search allowClear placeholder="搜索工具名或说明" style={{ width: 240 }}
                      value={kw} onChange={(e) => setKw(e.target.value)} />
      </Space>
      <Paragraph type="secondary" style={{ fontSize: '.78em', marginBottom: 10 }}>
        关闭开关可临时禁用某工具，Agent 执行时将不可调用。徽标标明来源：
        <SourceBadge source="builtin" /> 随软件内置 · <SourceBadge source="mcp" /> 由 MCP 服务器提供 ·
        <SourceBadge source="plugin" /> 由插件注册。
      </Paragraph>

      {shown.length ? (
        <div className="ent-grid">
          {shown.map((t) => (
            <EntityCard
              key={t.name}
              state={t.enabled ? undefined : 'off'}
              icon={TOOL_ICONS[t.name] || (t.source === 'mcp' ? '🔌' : t.source === 'plugin' ? '🧩' : '🛠')}
              title={<>{toolLabel(t.name)}
                <span className="ent-alias">{t.name}</span></>}
              badges={<>
                <SourceBadge source={t.source} />
                <Badge tone={t.tier}>{TIER_LABEL[t.tier] || t.tier}</Badge>
                <span className="risk-dot" title={`风险分 ${t.risk}`} style={{
                  background: t.risk >= 80 ? 'var(--red)' : t.risk >= 40 ? 'var(--yellow)' : 'var(--green)',
                }} />
              </>}
              desc={toolDesc(t.name, t.description)}
              meta={(t.params || []).length > 0 && (
                <>参数：{t.params.slice(0, 8).map((p: string) => <code key={p}>{p}</code>)}
                  {t.params.length > 8 && <span> 等 {t.params.length} 项</span>}</>
              )}
              actions={
                <Switch size="small" checked={t.enabled} onChange={async (v) => {
                  await apiPost('/tools/toggle', { name: t.name, enabled: v });
                  message.info(v ? `已启用 ${t.name}` : `已禁用 ${t.name}`);
                  reload();
                }} />
              }
            />
          ))}
        </div>
      ) : (
        <EmptyState icon="🔍" title="没有匹配的工具"
                    hint={kw ? `换个关键词试试，或把筛选切回「全部」。` : '该分类下暂时没有工具。'} />
      )}
    </>
  );
}

function SkillsTab() {
  const { message, modal } = App.useApp();
  const [skills, setSkills] = useState<any[]>([]);
  const [dir, setDir] = useState('');
  const reload = () => apiGet('/skills').then(setSkills).catch(() => {});
  useEffect(() => { reload(); }, []);
  return (
    <>
      <Card size="small" style={{ marginBottom: 10 }}>
        <Text strong style={{ fontSize: '.9em' }}>➕ 添加技能</Text>
        <Paragraph type="secondary" style={{ fontSize: '.76em', margin: '4px 0 8px' }}>
          支持 SKILL.md 技能包（文件夹含 SKILL.md）与 .py 技能（含 AbstractSkill 子类）。
        </Paragraph>
        <Space.Compact style={{ width: '100%' }}>
          <Input placeholder="技能目录，如 C:\Users\you\Desktop\skills" value={dir} onChange={(e) => setDir(e.target.value)} />
          <Button type="primary" onClick={async () => {
            if (!dir.trim()) { message.error('请输入技能目录'); return; }
            const r = await apiPost('/skills/load', { directory: dir.trim() });
            if (r.error) { message.error(r.error); return; }
            message.success(`已加载 ${r.loaded} 个技能（共 ${r.total}）`);
            reload();
          }}>📁 加载目录</Button>
          <Upload showUploadList={false} accept=".py" beforeUpload={async (f) => {
            const code = await f.text();
            const r = await apiPost('/skills/import', { name: f.name, code });
            if (r.error) message.error(r.error);
            else { message.success(`已导入技能: ${(r.imported || []).join(', ')}`); reload(); }
            return false;
          }}>
            <Button>📄 导入 .py</Button>
          </Upload>
        </Space.Compact>
        <Button style={{ marginTop: 8, width: '100%' }} onClick={async () => {
          message.info('正在导入桌面 skills 文件夹...');
          for (const d of ['~/Desktop/skills', '~/桌面/skills', '~/skills']) {
            const r = await apiPost('/skills/load', { directory: d });
            if (!r.error) {
              message.success(`已导入 ${r.loaded} 个技能（SKILL.md ${r.markdown || 0} · Python ${r.py || 0}）`);
              reload();
              return;
            }
          }
          message.error('未找到桌面 skills 文件夹，请用「加载目录」手动指定');
        }}>⬇️ 一键导入桌面 skills 文件夹</Button>
      </Card>
      {(['builtin', 'custom'] as const).map((group) => {
        const list = skills.filter((s) => (group === 'builtin' ? s.builtin : !s.builtin));
        if (!list.length) return null;
        return (
          <div key={group}>
            <SectionTitle count={list.length}>
              {group === 'builtin' ? '📦 内置技能（随软件分发）' : '✎ 自定义技能（你导入的）'}
            </SectionTitle>
            <div className="ent-grid">
              {list.map((s) => (
                <EntityCard
                  key={s.name}
                  icon={s.emoji || '✨'}
                  title={<>{toolLabel(s.name, SKILL_META)}
                    <span className="ent-alias">{s.name}</span></>}
                  badges={<SourceBadge source={s.builtin ? 'builtin' : s.type === 'markdown' ? 'markdown' : 'python'} />}
                  desc={toolDesc(s.name, s.description || '(无描述)', SKILL_META)}
                  meta={(s.required_tools || []).length > 0 && (
                    <>依赖工具：{s.required_tools.map((t: string) => <code key={t}>{t}</code>)}</>
                  )}
                  actions={!s.builtin && (
                    <Button size="small" danger type="text" onClick={() => modal.confirm({
                      title: `确定删除技能「${s.name}」？`,
                      onOk: async () => {
                        const r = await apiDelete(`/skills/${encodeURIComponent(s.name)}`);
                        if (r.error) message.error(r.error); else { message.info('已删除'); reload(); }
                      },
                    })}>🗑</Button>
                  )}
                />
              ))}
            </div>
          </div>
        );
      })}
    </>
  );
}

function McpPresetCard({ p, onAdded }: { p: any; onAdded: () => void }) {
  const { message } = App.useApp();
  const [url, setUrl] = useState('');
  const [needUrl, setNeedUrl] = useState(false);
  const add = async () => {
    if (p.transport === 'sse' && !p.url && !url.trim()) {
      setNeedUrl(true); message.warning('该服务需填写你的 MCP 端点 URL'); return;
    }
    const r = await apiPost('/mcp', {
      name: p.id, command: p.command, args: p.args,
      transport: p.transport, url: p.url || url.trim(),
    });
    if (r.error && !r.status) { message.error(r.error); return; }
    message.success(r.connected ? `${p.name} 已连接` : `${p.name} 已保存（${r.error || '未连接'}）`);
    onAdded();
  };
  return (
    <EntityCard
      icon={p.emoji || '🔌'}
      title={p.name}
      badges={<><Badge tone="mcp">官方适配</Badge><Badge tone="muted">{p.category}</Badge></>}
      desc={p.description}
      meta={<>
        {(p.env_hints || []).length > 0 && (
          <div>需环境变量：{p.env_hints.map((h: any) => (
            <code key={h.key} title={h.note}>{h.key}</code>
          ))}</div>
        )}
        {(needUrl || (p.transport === 'sse' && !p.url)) && (
          <Input size="small" style={{ marginTop: 6 }} value={url}
            placeholder="MCP 端点 URL（https://...）"
            onChange={(e) => setUrl(e.target.value)} />
        )}
      </>}
      actions={<>
        {p.docs && <Button size="small" type="link" onClick={() => window.open(p.docs, '_blank')}>文档 ↗</Button>}
        <Button size="small" type="primary" onClick={add}>➕ 添加</Button>
      </>}
    />
  );
}

function McpTab() {
  const { message, modal } = App.useApp();
  const [data, setData] = useState<any>({ servers: [] });
  const [presets, setPresets] = useState<any[]>([]);
  const [importText, setImportText] = useState('');
  const [form, setForm] = useState({ name: '', transport: 'stdio', command: '', args: '', url: '' });
  const reload = () => apiGet('/mcp').then(setData).catch(() => {});
  useEffect(() => {
    reload();
    apiGet('/mcp/presets').then((r) => setPresets(r?.presets || [])).catch(() => {});
  }, []);
  return (
    <>
      {!data.sdk_installed && (
        <Card size="small" style={{ marginBottom: 10, borderColor: 'var(--yellow)' }}>
          ⚠ 未检测到 MCP SDK，服务器可保存但无法连接。请先执行 <code>pip install mcp</code>。
        </Card>
      )}
      <Card size="small" style={{ marginBottom: 10 }}>
        <Text strong style={{ fontSize: '.9em' }}>⭐ 官方适配（一键添加）</Text>
        <Paragraph type="secondary" style={{ fontSize: '.76em', margin: '4px 0 8px' }}>
          点「添加」即写入配置并尝试连接；需要密钥的服务请先在系统环境变量里设置（详见各卡片）。
        </Paragraph>
        <div className="ent-grid wide">
          {presets.map((p) => <McpPresetCard key={p.id} p={p} onAdded={reload} />)}
        </div>
      </Card>
      <Card size="small" style={{ marginBottom: 10 }}>
        <Text strong style={{ fontSize: '.9em' }}>📥 批量导入 (Claude Desktop 格式)</Text>
        <Input.TextArea style={{ marginTop: 8 }} rows={3} value={importText} onChange={(e) => setImportText(e.target.value)}
          placeholder='{"mcpServers": {"filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}}}' />
        <Space style={{ marginTop: 8, width: '100%', justifyContent: 'space-between' }}>
          <Upload showUploadList={false} accept=".json" beforeUpload={async (f) => {
            setImportText(await f.text());
            message.info('已读取文件，点击「导入并连接」');
            return false;
          }}><Button>📄 选择文件</Button></Upload>
          <Button type="primary" onClick={async () => {
            if (!importText.trim()) { message.error('请粘贴 MCP 配置'); return; }
            let cfg;
            try { cfg = JSON.parse(importText); } catch (e: any) { message.error('JSON 解析失败: ' + e.message); return; }
            const r = await apiPost('/mcp/import', { config: cfg });
            if (r.error) { message.error(r.error); return; }
            message.success(`已导入 ${r.imported} 个服务器${r.connected_any ? '（部分已连接）' : ''}`);
            reload();
          }}>导入并连接</Button>
        </Space>
      </Card>
      <Card size="small" style={{ marginBottom: 10 }}>
        <Text strong style={{ fontSize: '.9em' }}>➕ 添加单个 MCP 服务器</Text>
        <Space.Compact style={{ width: '100%', marginTop: 8 }}>
          <Input style={{ width: 150 }} placeholder="名称" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <Input style={{ width: 100 }} placeholder="stdio/sse" value={form.transport} onChange={(e) => setForm({ ...form, transport: e.target.value })} />
          {form.transport === 'sse' ? (
            <Input placeholder="SSE URL" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
          ) : (
            <>
              <Input style={{ width: 110 }} placeholder="命令 (npx)" value={form.command} onChange={(e) => setForm({ ...form, command: e.target.value })} />
              <Input placeholder="参数 (空格分隔)" value={form.args} onChange={(e) => setForm({ ...form, args: e.target.value })} />
            </>
          )}
          <Button type="primary" onClick={async () => {
            if (!form.name.trim()) { message.error('请输入服务器名称'); return; }
            const r = await apiPost('/mcp', form);
            if (r.error && !r.status) { message.error(r.error); return; }
            message.info(r.connected ? 'MCP 已连接' : '已保存，但' + (r.error || '未连接'));
            reload();
          }}>添加并连接</Button>
        </Space.Compact>
      </Card>
      <SectionTitle count={(data.servers || []).length}>🔗 已添加的 MCP 服务器</SectionTitle>
      {(data.servers || []).length ? (
        <div className="ent-grid wide">
          {(data.servers || []).map((s: any) => (
            <EntityCard
              key={s.name}
              state={s.connected ? 'ok' : 'bad'}
              icon="🔌"
              title={s.name}
              badges={<Badge tone={s.connected ? 'builtin' : 'dangerous'}>
                {s.connected ? '已连接' : '未连接'}
              </Badge>}
              desc={<span className="mono">{s.transport} · {s.command || s.url} {(s.args || []).join(' ')}</span>}
              meta={s.tools?.length > 0 && (
                <>提供工具：{s.tools.slice(0, 10).map((t: string) => <code key={t}>{t}</code>)}
                  {s.tools.length > 10 && <span> 等 {s.tools.length} 个</span>}</>
              )}
              actions={
                <Button size="small" danger type="text" onClick={() => modal.confirm({
                  title: `确定删除 MCP 服务器「${s.name}」？`,
                  onOk: async () => { await apiDelete(`/mcp/${encodeURIComponent(s.name)}`); message.info('已删除'); reload(); },
                })}>🗑</Button>
              }
            />
          ))}
        </div>
      ) : (
        <EmptyState icon="🔌" title="还没有添加 MCP 服务器"
                    hint="从上方「官方适配」里挑一个点「添加」即可；也可以粘贴 Claude Desktop 的配置批量导入。" />
      )}
    </>
  );
}

function PluginsTab() {
  const { message } = App.useApp();
  const [data, setData] = useState<any>({ plugins: [] });
  const reload = () => apiGet('/plugins').then(setData).catch(() => {});
  useEffect(() => { reload(); }, []);
  const plugins = data.plugins || [];
  const toggle = async (p: any) => {
    const act = p.loaded ? 'unload' : 'load';
    const r = await apiPost(`/plugins/${encodeURIComponent(p.name)}/${act}`);
    if (r.error) message.error(r.error);
    else message[p.loaded ? 'info' : 'success'](`${p.loaded ? '已停用' : '已启用'}插件 ${p.name}`);
    reload();
  };
  return (
    <>
      <div className="tile-grid">
        <Tile label="🧩 插件总数" value={plugins.length}
              foot={`${plugins.filter((p: any) => p.loaded).length} 个已启用`} />
        <Tile label="📦 内置插件" value={plugins.filter((p: any) => p.builtin).length}
              tone="green" foot="随软件分发，默认启用" />
        <Tile label="✎ 自定义插件" value={plugins.filter((p: any) => !p.builtin).length}
              foot="来自 ~/.automind/plugins" />
      </div>
      <Card size="small" style={{ marginBottom: 12 }}>
        <Space style={{ width: '100%', justifyContent: 'space-between' }} align="start">
          <Paragraph type="secondary" style={{ fontSize: '.78em', margin: 0, maxWidth: 620 }}>
            插件通过生命周期钩子介入任务执行（任务开始/结束、工具调用前后等）。
            内置插件随软件分发、默认启用；自定义插件放在
            <code> ~/.automind/plugins/&lt;名称&gt;/ </code>下，需包含
            <code> plugin.json </code>与<code> hooks.py</code>（提供 <code>get_hooks() → AgentHooks</code>）。
            <b> 启用状态会被记住</b>，重启或切换模型后依然保持。
          </Paragraph>
          <Button size="small" onClick={reload}>🔄 重新扫描</Button>
        </Space>
      </Card>

      {(['builtin', 'custom'] as const).map((group) => {
        const list = plugins.filter((p: any) => (group === 'builtin' ? p.builtin : !p.builtin));
        if (!list.length) return null;
        return (
          <div key={group}>
            <SectionTitle count={list.length}>
              {group === 'builtin' ? '📦 内置插件' : '✎ 自定义插件'}
            </SectionTitle>
            <div className="ent-grid wide">
              {list.map((p: any) => (
                <EntityCard
                  key={p.name}
                  state={p.loaded ? undefined : 'off'}
                  icon="🧩"
                  title={p.name}
                  badges={<>
                    <SourceBadge source={p.builtin ? 'builtin' : 'custom'} />
                    {p.version && <Badge tone="muted">v{p.version}</Badge>}
                  </>}
                  desc={p.description || '(无描述)'}
                  meta={p.author && <>作者：{p.author}</>}
                  actions={<Switch size="small" checked={p.loaded} onChange={() => toggle(p)} />}
                />
              ))}
            </div>
          </div>
        );
      })}
      {!plugins.length && (
        <EmptyState icon="🧩" title="未发现任何插件"
                    hint={<>内置插件本应随软件分发；若这里是空的，多半是安装包不完整。
                      自定义插件请放在 <code>~/.automind/plugins</code> 下再点「重新扫描」。</>} />
      )}
    </>
  );
}

export default function ToolsView() {
  return (
    <div>
      <ViewHead icon="🔧" title="工具面板"
                sub="Agent 能调用的全部能力都在这里。徽标标明来源：内置随软件分发，MCP 来自你接入的服务器，插件由已启用的插件注册。" />
      <Tabs items={[
        { key: 'tools', label: '🔧 工具', children: <ToolsTab /> },
        { key: 'skills', label: '✨ 技能', children: <SkillsTab /> },
        { key: 'mcp', label: '🔌 MCP', children: <McpTab /> },
        { key: 'plugins', label: '🧩 插件', children: <PluginsTab /> },
      ]} />
    </div>
  );
}
