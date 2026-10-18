// 🔀 工作流（v1.8.0，R-108）：列表 → 在线编辑与校验 → 按入参生成表单 → 试运行 → 运行
// → 审批（复用全局审批弹窗）→ 看报告 / 续跑。不写代码、不开命令行。
// v1.9.0（专业版）：⏰ 触发器（定时 / webhook 的启停与令牌）、📊 看板（按工作流统计）。
// v1.10.0：📦 模板库 / 🧭 从执行轨迹生成（专业版）；🛡 发布审批、按工作流权限、🔑 凭据与
// 并发配额、运行审计导出（企业版）。
//
// 文件就是交付物：这里编辑的是项目 workflows/ 目录下的 YAML 原文，保存即落盘，
// 能进版本库、能 diff、能评审 —— 界面只是给它加了"写的时候就知道哪一行错了"。
import { App, Button, Empty, Input, Modal, Space, Table, Tabs, Tag, Tooltip } from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiGet, apiPost, errText } from '../../api/client';
import { chatSid } from '../../store/app';
import { RUN_STATUS_ZH, STEP_TYPE_ICON, TRIGGER_ZH, useWorkflow } from '../../store/workflow';
import FromTraceModal from '../workflow/FromTraceModal';
import GovernancePanel, { PUBLISH_TAG, type PublishState } from '../workflow/GovernancePanel';
import InputsForm, { collectInputs, initialDraft, type InputDraft } from '../workflow/InputsForm';
import SecretsModal from '../workflow/SecretsModal';
import TemplatesModal, { saveAsTemplate } from '../workflow/TemplatesModal';
import TriggersTab from '../workflow/TriggersTab';
import WorkflowDashboard from '../workflow/WorkflowDashboard';
import WorkflowRunCard from '../workflow/WorkflowRunCard';

interface Issue { message: string; line?: number; column?: number; path?: string; suggestions?: string[] }
interface WfItem {
  file: string; name: string; description: string; valid: boolean; steps: number;
  step_types: string[]; inputs: Record<string, any>; errors: Issue[]; warnings: Issue[];
  runnable: boolean; blocked_reason: string; last_run?: any; running_run_id?: string;
  source_digest?: string;
  /** v1.9.0：文件里声明的触发器（运行时状态见「⏰ 触发器」页） */
  triggers?: any[];
  /** v1.10.0：引用的凭据名、当前身份的权限、发布状态（企业版才有） */
  secrets?: string[];
  can?: { edit: boolean; run: boolean; trigger: boolean; approve: boolean };
  publish?: PublishState;
}

const NEW_TEMPLATE = `version: 1
name: 新工作流
description: 用一句话说明这条流程做什么

inputs:
  who:
    type: string
    required: false
    default: 世界
    description: 打招呼的对象

steps:
  - id: write
    type: tool
    tool: file_write
    args:
      path: "workflows/out/hello.txt"
      content: "你好，{{ inputs.who }}"
    on_failure: abort

  - id: readback
    type: tool
    tool: file_read
    args:
      path: "workflows/out/hello.txt"
`;

const fmtTime = (ts?: number) => (ts ? new Date(ts * 1000).toLocaleString() : '');

function issueLine(i: Issue): number {
  const anyI = i as any;
  return Number(i.line ?? anyI.position?.line ?? 0) || 0;
}

/** 在原生 textarea 里选中第 n 行（点击错误定位用）。 */
function selectLine(el: HTMLTextAreaElement | null, line: number) {
  if (!el || line <= 0) return;
  const lines = el.value.split('\n');
  let start = 0;
  for (let i = 0; i < Math.min(line - 1, lines.length); i++) start += lines[i].length + 1;
  const end = start + (lines[line - 1]?.length || 0);
  el.focus();
  el.setSelectionRange(start, end);
  const lh = parseFloat(getComputedStyle(el).lineHeight) || 18;
  el.scrollTop = Math.max(0, (line - 4) * lh);
}

function IssueList({ errors, warnings, onJump }: {
  errors: Issue[]; warnings: Issue[]; onJump: (line: number) => void;
}) {
  if (!errors.length && !warnings.length) {
    return <div className="wf-ok">✓ 校验通过</div>;
  }
  const row = (i: Issue, level: 'error' | 'warning', k: number) => {
    const line = issueLine(i);
    return (
      <div key={level + k} className={`wf-issue wf-issue-${level}`}
        onClick={() => line && onJump(line)} style={{ cursor: line ? 'pointer' : 'default' }}>
        <span className="wf-issue-line">{line ? `第 ${line} 行` : '—'}</span>
        <span>{i.message}
          {i.suggestions && i.suggestions.length > 0 && (
            <span className="hint-text">（你是不是想写：{i.suggestions.join('、')}）</span>
          )}
        </span>
      </div>
    );
  };
  return (
    <div className="wf-issues">
      {errors.map((i, k) => row(i, 'error', k))}
      {warnings.map((i, k) => row(i, 'warning', k))}
    </div>
  );
}

function DryRunTable({ report }: { report: any }) {
  const rows = (report?.steps || []).map((s: any) => {
    const d = s.detail || {};
    let what = '';
    if (s.type === 'tool') what = `调用 ${d.tool} ` + JSON.stringify(d.args || {}).slice(0, 200);
    else if (s.type === 'llm') what = `模型生成：${String(d.prompt_preview || '').slice(0, 160)}`;
    else if (s.type === 'human') what = `等待审批：${String(d.prompt_preview || '').slice(0, 160)}`;
    else if (s.type === 'branch') what = `判断 ${d.condition} → 成立 ${d.then} / 否则 ${d.else}`;
    else if (s.type === 'agent') {
      what = `Agent：${String(d.goal_preview || '').slice(0, 140)}；工具 `
        + (d.allowed_tools == null ? '仅只读' : (d.allowed_tools || []).join('、') || '无')
        + `；最多 ${d.max_steps} 步`;
    } else if (s.type === 'foreach') {
      what = `逐项执行 ${d.items} 项（上限 ${d.limit ?? '不限'}）`;
    } else if (s.type === 'parallel') {
      what = `并发执行 ${(d.children || []).map((c: any) => c.id).join('、')}`
        + `（最多同时 ${d.max_concurrency} 个）`;
    }
    return { key: s.id, ...s, what };
  });
  return (
    <Table size="small" pagination={false} dataSource={rows} style={{ marginTop: 8 }}
      columns={[
        { title: '步骤', dataIndex: 'id', width: 150,
          render: (v: string, r: any) => <span>{STEP_TYPE_ICON[r.type] || '•'} <code>{v}</code></span> },
        { title: '将执行什么', dataIndex: 'what',
          render: (v: string, r: any) => (r.status === 'ok'
            ? <span className="mono" style={{ fontSize: '.8em', overflowWrap: 'anywhere' }}>{v}</span>
            : <span className="wf-step-error">{r.error}</span>) },
      ]} />
  );
}

function RunsTab({ file, onOpenRun, limits, audit }: {
  file: string; onOpenRun: (id: string) => void; limits: Record<string, any>;
  /** 企业版：每次运行可导出审计（JSON / PDF） */
  audit: boolean;
}) {
  const { message } = App.useApp();
  const [rows, setRows] = useState<any[] | null>(null);
  const [err, setErr] = useState('');
  const resume = useWorkflow((s) => s.resume);
  const load = useCallback(() => {
    setErr('');
    apiGet(`/workflow/runs?file=${encodeURIComponent(file)}&limit=50`)
      .then((d) => setRows(d.runs || []))
      .catch((e) => { setErr(errText(e)); setRows([]); });
  }, [file]);
  useEffect(() => { load(); }, [load]);
  return (
    <div>
      <Space style={{ marginBottom: 8 }}>
        <Button size="small" onClick={load}>刷新</Button>
        <span className="hint-text">
          {limits.workflow_history_days ? `保留最近 ${limits.workflow_history_days} 天的运行记录`
            : limits.workflow_history ? `保留最近 ${limits.workflow_history} 次运行记录`
              : '运行记录按配置保留'}
        </span>
      </Space>
      {err && <div className="wf-step-error">{err}</div>}
      <Table size="small" loading={rows === null} dataSource={(rows || []).map((r) => ({ key: r.run_id, ...r }))}
        pagination={{ pageSize: 10, size: 'small' }}
        locale={{ emptyText: '还没有运行记录' }}
        columns={[
          { title: '开始时间', dataIndex: 'started_at', width: 170, render: fmtTime },
          { title: '状态', dataIndex: 'status', width: 110,
            render: (v: string, r: any) => (
              <Tag color={v === 'completed' ? 'success' : v === 'running' ? 'processing'
                : v === 'partial' ? 'warning' : 'default'}>
                {/* 已续跑 / 已忽略 / 运行中 以记录状态为准；其余显示那次运行本身的结果 */}
                {['running', 'resumed', 'dismissed'].includes(v) || !RUN_STATUS_ZH[r.run_status]
                  ? RUN_STATUS_ZH[v] || v : RUN_STATUS_ZH[r.run_status]}
              </Tag>) },
          { title: '来源', dataIndex: 'trigger', width: 90,
            render: (v: string, r: any) => (
              <span className="hint-text">{TRIGGER_ZH[v || 'manual'] || v}
                {r.attempt > 1 ? ` · 第 ${r.attempt} 次` : ''}</span>) },
          { title: '进度', width: 80,
            render: (_: any, r: any) => (r.steps_total ? `${r.steps_done}/${r.steps_total}` : r.steps_done) },
          { title: '说明', dataIndex: 'error', ellipsis: true,
            render: (v: string, r: any) => (
              <span className="hint-text">{r.resumed_from ? `↻ 续跑自 ${r.resumed_from}　` : ''}{v}</span>) },
          { title: '', width: 210,
            render: (_: any, r: any) => (
              <Space size={4}>
                <Button size="small" type="link" onClick={() => onOpenRun(r.run_id)}>查看</Button>
                {audit && r.status !== 'running' && (
                  <Tooltip title="运行审计：输入、发起人、每步状态与输出摘要、审批人与时间">
                    <Button size="small" type="link" onClick={() => window.open(
                      `/api/workflow/runs/${encodeURIComponent(r.run_id)}/audit?format=pdf`, '_blank')}>审计</Button>
                  </Tooltip>
                )}
                {r.resumable && (
                  <Button size="small" type="link" onClick={async () => {
                    try {
                      const nid = await resume(r.run_id, r.session_id || chatSid());
                      message.success('已从断点续跑');
                      onOpenRun(nid);
                      load();
                    } catch (e) { message.error(errText(e), 6); }
                  }}>↻ 续跑</Button>
                )}
                {r.resumable && (
                  <Button size="small" type="link" onClick={async () => {
                    try { await apiPost(`/workflow/runs/${r.run_id}/dismiss`); load(); }
                    catch (e) { message.error(errText(e)); }
                  }}>忽略</Button>
                )}
              </Space>) },
        ]} />
    </div>
  );
}

export default function WorkflowView() {
  const { message, modal } = App.useApp();
  const [list, setList] = useState<WfItem[] | null>(null);
  const [limits, setLimits] = useState<Record<string, any>>({});
  const [dir, setDir] = useState('');
  const [loadErr, setLoadErr] = useState('');
  const [sel, setSel] = useState<string>('');           // 选中的文件名；'' = 未选；'__new__' = 新建
  const [newName, setNewName] = useState('');
  const [text, setText] = useState('');
  const [saved, setSaved] = useState('');
  const [issues, setIssues] = useState<{ errors: Issue[]; warnings: Issue[]; schema?: any }>({ errors: [], warnings: [] });
  const [tab, setTab] = useState('run');
  const [draft, setDraft] = useState<InputDraft>({});
  const [dry, setDry] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [runId, setRunId] = useState('');
  const [viewRun, setViewRun] = useState('');
  const [showDash, setShowDash] = useState(false);
  const [showTpl, setShowTpl] = useState(false);
  const [showTrace, setShowTrace] = useState(false);
  const [showSecrets, setShowSecrets] = useState(false);
  const edRef = useRef<HTMLTextAreaElement | null>(null);
  const runs = useWorkflow((s) => s.runs);

  const loadList = useCallback(async () => {
    setLoadErr('');
    try {
      const d = await apiGet('/workflow/list');
      setList(d.workflows || []);
      setLimits(d.limits || {});
      setDir(d.dir || '');
    } catch (e) { setLoadErr(errText(e)); setList([]); }
  }, []);
  useEffect(() => { loadList(); }, [loadList]);

  const item = useMemo(() => (list || []).find((w) => w.file === sel), [list, sel]);
  const dirty = text !== saved;
  const isNew = sel === '__new__';

  // 选中文件 → 读原文；输入框草稿按声明重置
  useEffect(() => {
    setDry(null); setRunId('');
    if (!sel || isNew) return;
    apiGet(`/workflow/source?file=${encodeURIComponent(sel)}`)
      .then((d) => { setText(d.yaml || ''); setSaved(d.yaml || ''); })
      .catch((e) => message.error(errText(e)));
  }, [sel]); // eslint-disable-line react-hooks/exhaustive-deps
  // 只在"换了一份工作流 / 文件内容变了"时重置草稿 —— 列表刷新（例如一次运行结束）
  // 会换掉 item 对象本身，若按对象判断，用户刚填的入参会被清空
  useEffect(() => { setDraft(initialDraft(item?.inputs || {})); },
    [item?.file, item?.source_digest]); // eslint-disable-line react-hooks/exhaustive-deps
  // 正在跑的那次（例如从别处发起的）自动挂到运行面板上
  useEffect(() => { if (item?.running_run_id) setRunId(item.running_run_id); }, [item?.running_run_id]);

  // 编辑时实时校验（防抖）：错误定位到行
  useEffect(() => {
    if (!sel) return;
    const t = window.setTimeout(() => {
      if (!text.trim()) { setIssues({ errors: [{ message: '内容为空' }], warnings: [] }); return; }
      apiPost('/workflow/validate', { yaml: text, source: isNew ? '<新建>' : sel })
        .then((r) => setIssues({ errors: r.errors || [], warnings: r.warnings || [], schema: r.schema }))
        .catch(() => { /* 校验接口失败不打断编辑 */ });
    }, 500);
    return () => window.clearTimeout(t);
  }, [text, sel, isNew]);

  const confirmLeave = (then: () => void) => {
    if (!dirty) { then(); return; }
    modal.confirm({
      title: '有未保存的修改', content: '离开后这些修改会丢失。', okText: '放弃修改',
      okButtonProps: { danger: true }, onOk: then,
    });
  };

  const openNew = () => setShowTpl(true);
  // 从模板 / 轨迹草稿新建：内容先放进编辑器，保存时才落盘
  const openDraft = (yaml: string, fileName = '') => confirmLeave(() => {
    setSel('__new__'); setNewName(fileName); setText(yaml); setSaved(''); setTab('edit');
  });

  const save = async () => {
    const file = isNew ? newName.trim() : sel;
    if (!file) { message.error('请先填写文件名（如 daily_report.yaml）'); return; }
    try {
      const r = await apiPost('/workflow/save', { file, yaml: text, overwrite: !isNew });
      setSaved(text);
      message.success(r.valid ? `已保存 ${r.file}` : `已保存 ${r.file}（校验未通过，暂不能运行）`);
      await loadList();
      setSel(r.file);
    } catch (e) { message.error(errText(e), 6); }
  };

  const remove = () => modal.confirm({
    title: `删除工作流 ${sel}？`, content: '文件将从 workflows/ 目录删除；已有的运行记录保留。',
    okButtonProps: { danger: true }, okText: '删除',
    onOk: async () => {
      try {
        await apiPost('/workflow/delete', { file: sel });
        message.success('已删除');
        setSel(''); setText(''); setSaved('');
        loadList();
      } catch (e) { message.error(errText(e)); }
    },
  });

  const run = async (dryRun: boolean) => {
    if (!item) return;
    if (dirty) { message.warning('有未保存的修改：运行的是磁盘上的版本，请先保存'); return; }
    const [inputs, errors] = collectInputs(item.inputs || {}, draft);
    if (errors.length) { message.error(errors[0]); return; }
    setBusy(true);
    try {
      const r = await apiPost('/workflow/run', { file: item.file, inputs, dry_run: dryRun, session_id: chatSid() });
      if (dryRun) { setDry(r); setRunId(''); }
      else {
        setDry(null); setRunId(r.run_id);
        if (!useWorkflow.getState().runs[r.run_id]) {
          useWorkflow.getState().fetchRun(r.run_id).catch(() => { /* WS 事件会补上 */ });
        }
        message.success('已开始运行；需要审批的步骤会弹窗询问');
      }
    } catch (e) { message.error(errText(e), 6); }
    finally { setBusy(false); }
  };

  // 运行结束 → 刷新列表里的"最近运行"
  const curRun = runId ? runs[runId] : undefined;
  useEffect(() => {
    if (curRun && curRun.status !== 'running') loadList();
  }, [curRun?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const lim = limits || {};
  const validCount = (list || []).filter((w) => w.valid).length;

  return (
    <div className="wf-view">
      <div className="wf-list">
        <div className="wf-list-head">
          <h3 style={{ margin: 0 }}>🔀 工作流</h3>
          <Space size={4} wrap>
            <Tooltip title="按工作流统计成功率、耗时、Token 与趋势（专业版）">
              <Button size="small" onClick={() => setShowDash(true)}>📊 看板</Button>
            </Tooltip>
            <Tooltip title="把一次成功的任务整理成工作流草稿（专业版）">
              <Button size="small" onClick={() => setShowTrace(true)}>🧭</Button>
            </Tooltip>
            <Tooltip title="凭据与并发配额（企业版）">
              <Button size="small" onClick={() => setShowSecrets(true)}>🔑</Button>
            </Tooltip>
            <Button size="small" type="primary" onClick={openNew}>＋ 新建</Button>
          </Space>
        </div>
        <div className="hint-text" style={{ margin: '4px 0 8px' }}>
          {validCount} 个可用{lim.workflows != null ? ` / 上限 ${lim.workflows}` : ''}
          {lim.workflow_foreach_max != null ? ` · foreach ≤ ${lim.workflow_foreach_max} 项` : ''}
        </div>
        {loadErr && <div className="wf-step-error">{loadErr}</div>}
        {list === null && <div className="hint-text">正在读取…</div>}
        {list && !list.length && (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={<span className="hint-text">{dir || 'workflows/'} 下还没有工作流</span>} />
        )}
        {(list || []).map((w) => (
          <div key={w.file} className={'wf-item' + (w.file === sel ? ' active' : '')}
            onClick={() => w.file !== sel && confirmLeave(() => { setSel(w.file); setTab('run'); })}>
            <div className="wf-item-title">
              {w.name}
              {!w.valid && <Tag color="error">校验未通过</Tag>}
              {w.valid && !w.runnable && <Tooltip title={w.blocked_reason}><Tag color="warning">超出上限</Tag></Tooltip>}
              {w.running_run_id && <Tag color="processing">运行中</Tag>}
              {w.publish && (
                <Tag color={PUBLISH_TAG[w.publish.status]?.color}>{PUBLISH_TAG[w.publish.status]?.text}</Tag>
              )}
              {!!w.triggers?.length && (
                <Tooltip title={w.triggers.map((t: any) => (t.kind === 'cron' ? `⏰ ${t.cron}` : '🔗 webhook')).join('；')}>
                  <Tag color="purple">⏰ {w.triggers.length}</Tag>
                </Tooltip>
              )}
            </div>
            <div className="hint-text">
              {w.file} · {w.steps} 步 {w.step_types.map((t) => STEP_TYPE_ICON[t] || '').join('')}
            </div>
            {w.last_run && (
              <div className="hint-text">
                最近：{RUN_STATUS_ZH[w.last_run.run_status] || RUN_STATUS_ZH[w.last_run.status] || w.last_run.status}
                {' · '}{fmtTime(w.last_run.started_at)}
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="wf-detail">
        {!sel && (
          <div className="wf-empty">
            <p>工作流是一份写死了每一步的 YAML：可评审、可 diff、可进版本库。</p>
            <p className="hint-text">选择左侧的工作流运行它，或点「＋ 新建」从模板开始。
              步骤类型：🔧 tool · 💬 llm · 🙋 human · 🔀 branch · 🤖 agent · 🔁 foreach
              · ⚡ parallel（专业版）</p>
          </div>
        )}
        {sel && (
          <>
            <div className="wf-detail-head">
              {isNew ? (
                <Input style={{ maxWidth: 320 }} placeholder="文件名，如 daily_report.yaml"
                  value={newName} onChange={(e) => setNewName(e.target.value)} />
              ) : (
                <span><b>{item?.name || sel}</b> <span className="hint-text">{sel}</span></span>
              )}
              <Space style={{ marginLeft: 'auto' }}>
                {dirty && <Tag color="warning">未保存</Tag>}
                <Tooltip title="把当前内容存进模板库，以后「＋ 新建」时可以直接用（专业版）">
                  <Button size="small" disabled={!text.trim()} onClick={async () => {
                    try {
                      const n = await saveAsTemplate(text, item?.name || newName || '');
                      message.success(`已存为模板：${n}`);
                    } catch (e) { message.error(errText(e), 6); }
                  }}>存为模板</Button>
                </Tooltip>
                <Tooltip title={item?.can && !item.can.edit ? '没有编辑这份工作流的权限' : ''}>
                  <Button size="small" type={dirty ? 'primary' : 'default'} onClick={save}
                    disabled={(!dirty && !isNew) || (!!item?.can && !item.can.edit)}>保存</Button>
                </Tooltip>
                {!isNew && <Button size="small" danger onClick={remove}>删除</Button>}
              </Space>
            </div>
            {item?.description && <div className="hint-text" style={{ marginBottom: 6 }}>{item.description}</div>}
            <Tabs activeKey={isNew ? 'edit' : tab} onChange={setTab} size="small" items={[
              {
                key: 'run', label: '▶ 运行', disabled: isNew,
                children: item && (
                  <div>
                    {item.publish && (
                      <GovernancePanel file={item.file} state={item.publish} dirty={dirty}
                        canSubmit={!!item.can?.edit} canApprove={!!item.can?.approve}
                        onChanged={loadList} />
                    )}
                    {!!item.secrets?.length && (
                      <div className="hint-text" style={{ margin: '4px 0' }}>
                        🔑 需要凭据：{item.secrets.map((n) => <code key={n} style={{ marginRight: 4 }}>{n}</code>)}
                        （企业版「🔑」里配置；值不会进报告与日志）
                      </div>
                    )}
                    {!item.valid && <div className="wf-step-error">校验未通过，请先在「编辑」里修正。</div>}
                    {item.valid && !item.runnable && <div className="wf-step-error">{item.blocked_reason}</div>}
                    <InputsForm specs={item.inputs || {}} draft={draft} onChange={setDraft} />
                    <Space style={{ marginTop: 10 }}>
                      <Tooltip title="只渲染参数、列出将执行什么：不调工具、不调模型、不弹审批">
                        <Button disabled={!item.valid || busy} onClick={() => run(true)}>🧪 试运行</Button>
                      </Tooltip>
                      <Tooltip title={item.can && !item.can.run ? '没有运行这份工作流的权限' : ''}>
                        <Button type="primary" loading={busy}
                          disabled={!item.valid || !item.runnable || curRun?.status === 'running'
                            || (!!item.can && !item.can.run)}
                          onClick={() => run(false)}>▶ 运行</Button>
                      </Tooltip>
                      <span className="hint-text">运行计 1 次任务额度</span>
                    </Space>
                    {dry && (
                      <div style={{ marginTop: 10 }}>
                        <b>试运行计划</b> <span className="hint-text">{dry.summary?.failed ? `（${dry.summary.failed} 步渲染失败）` : '（全部可渲染，未执行任何动作）'}</span>
                        <DryRunTable report={dry} />
                      </div>
                    )}
                    {runId && (
                      <div style={{ marginTop: 12 }}>
                        <WorkflowRunCard runId={runId} onResumed={setRunId} />
                      </div>
                    )}
                  </div>
                ),
              },
              {
                key: 'edit', label: `✎ 编辑${issues.errors.length ? `（${issues.errors.length} 个错误）` : ''}`,
                children: (
                  <div className="wf-editor-wrap">
                    <textarea ref={edRef} className="wf-editor mono" spellCheck={false}
                      value={text} onChange={(e) => setText(e.target.value)}
                      onKeyDown={(e) => {
                        if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save(); }
                        if (e.key === 'Tab') {
                          e.preventDefault();
                          const el = e.currentTarget;
                          const { selectionStart: s, selectionEnd: t } = el;
                          const v = el.value.slice(0, s) + '  ' + el.value.slice(t);
                          setText(v);
                          requestAnimationFrame(() => el.setSelectionRange(s + 2, s + 2));
                        }
                      }} />
                    <IssueList errors={issues.errors} warnings={issues.warnings}
                      onJump={(l) => selectLine(edRef.current, l)} />
                    <div className="hint-text" style={{ marginTop: 4 }}>
                      Ctrl+S 保存 · 点击问题跳到对应行 · 字段说明见 docs/WORKFLOWS.md
                    </div>
                  </div>
                ),
              },
              {
                key: 'triggers',
                label: `⏰ 触发器${item?.triggers?.length ? `（${item.triggers.length}）` : ''}`,
                disabled: isNew,
                children: !isNew && (
                  <TriggersTab file={sel} declared={item?.triggers || []}
                    onOpenRun={(id) => setViewRun(id)} />
                ),
              },
              {
                key: 'runs', label: '🕘 运行记录', disabled: isNew,
                children: !isNew && <RunsTab file={sel} limits={lim} audit={!!item?.publish}
                  onOpenRun={(id) => setViewRun(id)} />,
              },
            ]} />
          </>
        )}
      </div>
      <TemplatesModal open={showTpl} onClose={() => setShowTpl(false)} starter={NEW_TEMPLATE}
        onPick={(yaml) => openDraft(yaml)}
        onInstalled={(file) => confirmLeave(async () => {
          await loadList(); setSel(file); setText(''); setSaved(''); setTab('run');
        })} />
      <FromTraceModal open={showTrace} onClose={() => setShowTrace(false)}
        onDraft={(yaml, file) => openDraft(yaml, file)} />
      <SecretsModal open={showSecrets} onClose={() => setShowSecrets(false)} />
      <Modal open={showDash} onCancel={() => setShowDash(false)} footer={null} width={1000}
        title="📊 工作流看板" destroyOnClose>
        <WorkflowDashboard onOpenFile={(f) => confirmLeave(() => {
          setShowDash(false); setSel(f); setTab('runs');
        })} />
      </Modal>
      <Modal open={!!viewRun} onCancel={() => setViewRun('')} footer={null} width={640}
        title="运行详情" destroyOnClose>
        {viewRun && <WorkflowRunCard runId={viewRun} onResumed={setViewRun} />}
      </Modal>
    </div>
  );
}
