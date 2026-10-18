// 📦 新建：从模板开始（v1.10.0，专业版模板库 R-308；社区版只有内置的起步样板）。
// 选中模板 → 把 YAML 放进编辑器当作新文件（还没落盘），保存时才进 workflows/。
import { App, Button, Empty, Input, List, Modal, Space, Tag, Upload } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost, errText } from '../../api/client';

export interface TemplateItem {
  id: string; name: string; description?: string; tags?: string[]; yaml: string; builtin?: boolean;
}

/** v1.11.5 随包分发的可运行样板（社区版可用；可能带知识库等配套文件，所以是"安装"而不是"新建草稿"） */
interface SampleItem {
  id: string; file: string; name: string; description?: string; tags?: string[];
  extras?: string[]; yaml: string; installed?: boolean;
}

export default function TemplatesModal({ open, onClose, onPick, onInstalled, starter }: {
  open: boolean; onClose: () => void;
  onPick: (yaml: string, name: string) => void;
  /** 样板安装完成（或已安装）→ 打开该文件 */
  onInstalled?: (file: string) => void;
  /** 社区版也有的那份起步样板 */
  starter: string;
}) {
  const { message } = App.useApp();
  const [items, setItems] = useState<TemplateItem[]>([]);
  const [locked, setLocked] = useState('');
  const [filter, setFilter] = useState('');
  const [samples, setSamples] = useState<SampleItem[]>([]);
  const [installing, setInstalling] = useState('');

  const load = useCallback(async () => {
    try {
      const d = await apiGet('/workflow/templates');
      if (d?.feature) { setLocked(String(d.error || '')); setItems([]); return; }
      setLocked('');
      setItems(d.templates || []);
    } catch (e) { message.error(errText(e)); }
  }, [message]);
  const loadSamples = useCallback(async () => {
    try {
      const d = await apiGet('/workflow/samples');
      setSamples(d.samples || []);
    } catch { setSamples([]); }   // 旧版服务端没有这个接口：只是少了样板，不报错
  }, []);
  useEffect(() => { if (open) { load(); loadSamples(); } }, [open, load, loadSamples]);

  const install = async (s: SampleItem) => {
    if (s.installed) { onInstalled?.(s.file); onClose(); return; }
    setInstalling(s.id);
    try {
      const r = await apiPost(`/workflow/samples/${s.id}/install`, {});
      message.success(`已安装 ${r.file}` + (r.extras?.length ? `（含 ${r.extras.join('、')}）` : '')
        + '，在「运行」页填写客户留言即可试跑');
      onInstalled?.(r.file); onClose();
    } catch (e) { message.error(errText(e), 6); } finally { setInstalling(''); }
  };
  const shownSamples = samples.filter((t) => !filter
    || `${t.name}${t.description}${(t.tags || []).join('')}`.includes(filter));

  const exportAll = async () => {
    try {
      const r = await fetch('/api/workflow/templates/export');
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'automind-workflow-templates.json';
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 0);
    } catch (e) { message.error(errText(e)); }
  };
  const importFile = async (file: File) => {
    try {
      const bundle = JSON.parse(await file.text());
      const r = await apiPost('/workflow/templates/import', { bundle });
      message.success(`导入 ${r.added?.length || 0} 个，跳过重复 ${r.skipped?.length || 0} 个`
        + (r.failed?.length ? `，${r.failed.length} 个校验未通过` : ''), 6);
      load();
    } catch (e) { message.error(errText(e), 6); }
    return false;
  };

  const all: TemplateItem[] = [
    { id: '__starter__', name: '空白起步样板', description: '最小的两步流程：写文件、读回核对', yaml: starter, builtin: true },
    ...items.filter((t) => t.id !== 'builtin-file-pipeline'),
  ].filter((t) => !filter || `${t.name}${t.description}${(t.tags || []).join('')}`.includes(filter));

  return (
    <Modal open={open} onCancel={onClose} footer={null} width={680} title="＋ 新建工作流：从模板开始" destroyOnClose>
      <Space style={{ marginBottom: 8 }} wrap>
        <Input.Search size="small" allowClear placeholder="搜索模板" style={{ width: 220 }}
          onChange={(e) => setFilter(e.target.value)} />
        {!locked && <Button size="small" onClick={exportAll}>⬇ 导出我的模板</Button>}
        {!locked && (
          <Upload accept=".json" showUploadList={false} beforeUpload={importFile}>
            <Button size="small">⬆ 导入模板包</Button>
          </Upload>
        )}
      </Space>
      {locked && <div className="hint-text" style={{ marginBottom: 8 }}>
        🔒 自定义模板库（保存 / 导入导出）是专业版功能；社区版可以从起步样板开始。</div>}
      {shownSamples.length > 0 && (
        <List size="small" header={<b>▶ 可直接运行的内置样板</b>} dataSource={shownSamples}
          style={{ marginBottom: 10 }} renderItem={(t) => (
          <List.Item actions={[
            <Button key="ins" size="small" type="primary" loading={installing === t.id}
              onClick={() => install(t)}>{t.installed ? '打开' : '一键安装'}</Button>,
            <Button key="draft" size="small" type="link" onClick={() => { onPick(t.yaml, t.name); onClose(); }}>
              只看 YAML</Button>,
          ]}>
            <List.Item.Meta
              title={<span>{t.name} <Tag color="green">样板</Tag>
                {t.installed && <Tag>已安装</Tag>}
                {(t.tags || []).map((g) => <Tag key={g}>{g}</Tag>)}</span>}
              description={<span className="hint-text">{t.description}
                {t.extras?.length ? `（附带：${t.extras.join('、')}）` : ''}</span>} />
          </List.Item>
        )} />
      )}
      {!all.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的模板" /> : (
        <List size="small" dataSource={all} renderItem={(t) => (
          <List.Item actions={[
            <Button key="use" size="small" type="primary" onClick={() => { onPick(t.yaml, t.name); onClose(); }}>用它新建</Button>,
            ...(!t.builtin ? [<Button key="del" size="small" type="link" danger onClick={async () => {
              try { await apiDelete(`/workflow/templates/${t.id}`); load(); } catch (e) { message.error(errText(e)); }
            }}>删除</Button>] : []),
          ]}>
            <List.Item.Meta
              title={<span>{t.name} {t.builtin ? <Tag>内置</Tag> : <Tag color="blue">我的</Tag>}
                {(t.tags || []).map((g) => <Tag key={g}>{g}</Tag>)}</span>}
              description={<span className="hint-text">{t.description}</span>} />
          </List.Item>
        )} />
      )}
    </Modal>
  );
}

/** 把当前编辑器里的 YAML 存为模板（专业版）。 */
export async function saveAsTemplate(yaml: string, name: string): Promise<string> {
  const d = await apiPost('/workflow/templates', { yaml, name });
  if (d?.feature) throw new Error(String(d.error || '模板库是专业版功能'));
  return String(d.template?.name || name);
}
