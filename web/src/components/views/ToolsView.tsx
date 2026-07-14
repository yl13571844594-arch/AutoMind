// 🔧 工具面板：工具 / 技能 / MCP / 插件 四个分栏。
import { App, Button, Card, Input, Space, Switch, Tabs, Tag, Typography, Upload } from 'antd';
import { useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';

const { Text, Paragraph } = Typography;
const TOOL_ICONS: Record<string, string> = {
  terminal: '⌨️', file_read: '📖', file_write: '✍️', file_edit: '✏️',
  python_sandbox: '🐍', browser: '🌐', web_fetch: '🔗',
};

function ToolsTab() {
  const { message } = App.useApp();
  const [tools, setTools] = useState<any[]>([]);
  const reload = () => apiGet('/tools').then(setTools).catch(() => {});
  useEffect(() => { reload(); }, []);
  return (
    <>
      <Paragraph type="secondary" style={{ fontSize: '.82em' }}>
        {tools.filter((t) => t.enabled).length}/{tools.length} 启用 — 关闭开关可临时禁用某工具（Agent 执行时将不可调用）。
      </Paragraph>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(320px,1fr))', gap: 10 }}>
        {tools.map((t) => (
          <Card key={t.name} size="small" style={{ opacity: t.enabled ? 1 : .55 }}>
            <Space align="start" style={{ width: '100%' }}>
              <span style={{ fontSize: '1.4em' }}>{TOOL_ICONS[t.name] || (t.mcp ? '🔌' : '🛠')}</span>
              <div style={{ flex: 1 }}>
                <Text strong>{t.name}</Text>{' '}
                <Tag color={t.tier === 'dangerous' ? 'red' : t.tier === 'sensitive' ? 'gold' : 'green'}>{t.tier}</Tag>
                <span title={`风险 ${t.risk}`} style={{
                  display: 'inline-block', width: 8, height: 8, borderRadius: '50%',
                  background: t.risk >= 80 ? 'var(--red)' : t.risk >= 40 ? 'var(--yellow)' : 'var(--green)',
                }} />
                <div className="hint-text">{t.description}</div>
                {(t.params || []).length > 0 && (
                  <div className="hint-text" style={{ marginTop: 2 }}>参数: {t.params.map((p: string) => <code key={p} style={{ marginRight: 4 }}>{p}</code>)}</div>
                )}
              </div>
              <Switch size="small" checked={t.enabled} onChange={async (v) => {
                await apiPost('/tools/toggle', { name: t.name, enabled: v });
                message.info(v ? `已启用 ${t.name}` : `已禁用 ${t.name}`);
                reload();
              }} />
            </Space>
          </Card>
        ))}
      </div>
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
      {skills.map((s) => (
        <Card key={s.name} size="small" style={{ marginBottom: 8 }}>
          <Space align="start" style={{ width: '100%' }}>
            <span style={{ fontSize: '1.3em' }}>{s.emoji || '✨'}</span>
            <div style={{ flex: 1 }}>
              <Text strong>{s.name}</Text>{' '}
              {s.builtin ? <Tag color="green">内置</Tag> : s.type === 'markdown' ? <Tag color="purple">SKILL.md</Tag> : <Tag color="gold">Python</Tag>}
              <div className="hint-text">{s.description || '(无描述)'}</div>
              {(s.required_tools || []).length > 0 && <div className="hint-text">依赖工具: {s.required_tools.join(', ')}</div>}
            </div>
            {!s.builtin && (
              <Button size="small" danger type="text" onClick={() => modal.confirm({
                title: `确定删除技能「${s.name}」？`,
                onOk: async () => {
                  const r = await apiDelete(`/skills/${encodeURIComponent(s.name)}`);
                  if (r.error) message.error(r.error); else { message.info('已删除'); reload(); }
                },
              })}>🗑</Button>
            )}
          </Space>
        </Card>
      ))}
    </>
  );
}

function McpTab() {
  const { message, modal } = App.useApp();
  const [data, setData] = useState<any>({ servers: [] });
  const [importText, setImportText] = useState('');
  const [form, setForm] = useState({ name: '', transport: 'stdio', command: '', args: '', url: '' });
  const reload = () => apiGet('/mcp').then(setData).catch(() => {});
  useEffect(() => { reload(); }, []);
  return (
    <>
      {!data.sdk_installed && (
        <Card size="small" style={{ marginBottom: 10, borderColor: 'var(--yellow)' }}>
          ⚠ 未检测到 MCP SDK，服务器可保存但无法连接。请先执行 <code>pip install mcp</code>。
        </Card>
      )}
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
      {(data.servers || []).map((s: any) => (
        <Card key={s.name} size="small" style={{ marginBottom: 8, borderColor: s.connected ? 'var(--green)' : 'var(--red)' }}>
          <Space align="start" style={{ width: '100%' }}>
          <div style={{ flex: 1 }}>
            <Text strong>🔌 {s.name}</Text> <Tag color={s.connected ? 'green' : 'red'}>{s.connected ? '已连接' : '未连接'}</Tag>
            <div className="mono hint-text">{s.transport} · {s.command || s.url} {(s.args || []).join(' ')}</div>
            {s.tools?.length > 0 && <div className="hint-text">工具: {s.tools.join(', ')}</div>}
          </div>
          <Button size="small" danger type="text" onClick={() => modal.confirm({
            title: `确定删除 MCP 服务器「${s.name}」？`,
            onOk: async () => { await apiDelete(`/mcp/${encodeURIComponent(s.name)}`); message.info('已删除'); reload(); },
          })}>🗑</Button>
          </Space>
        </Card>
      ))}
      {!(data.servers || []).length && <em className="hint-text">暂无 MCP 服务器</em>}
    </>
  );
}

function PluginsTab() {
  const { message } = App.useApp();
  const [data, setData] = useState<any>({ plugins: [] });
  const reload = () => apiGet('/plugins').then(setData).catch(() => {});
  useEffect(() => { reload(); }, []);
  const plugins = data.plugins || [];
  return (
    <>
      <Card size="small" style={{ marginBottom: 10 }}>
        <Paragraph type="secondary" style={{ fontSize: '.8em', margin: 0 }}>
          把插件放在 <code>~/.automind/plugins/&lt;名称&gt;/</code> 下，每个插件包含 <code>plugin.json</code> 与 <code>hooks.py</code>（提供 <code>get_hooks() → AgentHooks</code>）。
        </Paragraph>
        <div style={{ textAlign: 'right' }}><Button size="small" onClick={reload}>🔄 重新扫描</Button></div>
      </Card>
      {plugins.map((p: any) => (
        <Card key={p.name} size="small" style={{ marginBottom: 8 }}>
          <Space align="start" style={{ width: '100%' }}>
            <div style={{ flex: 1 }}>
              <Text strong>🧩 {p.name}</Text> <Tag color={p.loaded ? 'green' : 'gold'}>{p.loaded ? '已加载' : '未加载'}</Tag>
              {p.version && <span className="hint-text">v{p.version}</span>}
              <div className="hint-text">{p.description || '(无描述)'}</div>
            </div>
            {p.loaded
              ? <Button size="small" onClick={async () => { await apiPost(`/plugins/${encodeURIComponent(p.name)}/unload`); message.info(`已卸载插件 ${p.name}`); reload(); }}>卸载</Button>
              : <Button size="small" type="primary" onClick={async () => {
                const r = await apiPost(`/plugins/${encodeURIComponent(p.name)}/load`);
                if (r.error) message.error(r.error); else { message.success(`已加载插件 ${p.name}`); reload(); }
              }}>加载</Button>}
          </Space>
        </Card>
      ))}
      {!plugins.length && <em className="hint-text">未发现插件。请在 ~/.automind/plugins 下放置插件目录后点击「重新扫描」。</em>}
    </>
  );
}

export default function ToolsView() {
  return (
    <div>
      <h3 style={{ marginBottom: 12 }}>🔧 工具面板</h3>
      <Tabs items={[
        { key: 'tools', label: '🔧 工具', children: <ToolsTab /> },
        { key: 'skills', label: '✨ 技能', children: <SkillsTab /> },
        { key: 'mcp', label: '🔌 MCP', children: <McpTab /> },
        { key: 'plugins', label: '🧩 插件', children: <PluginsTab /> },
      ]} />
    </div>
  );
}
