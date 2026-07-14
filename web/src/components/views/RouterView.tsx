// 🧭 路由与成本：语义缓存（专业版）+ 模型智能路由（专业 2 级 / 企业 N 级）
//               + 成本仪表盘（企业版）。社区版显示升级卡片。
import {
  App, Button, Card, Input, InputNumber, Select, Space, Statistic, Switch, Table, Tag, Typography,
} from 'antd';
import { useEffect, useState } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';
import { useApp } from '../../store/app';

const { Text, Paragraph } = Typography;

function UpgradeCard({ label, tier }: { label: string; tier: string }) {
  return (
    <Card size="small">
      🔒 <b>{label}</b> 为<b>{tier}</b>功能
      <div className="hint-text" style={{ marginTop: 4 }}>安装 automind-pro 并配置许可证（AUTOMIND_LICENSE）后重启服务即可解锁</div>
    </Card>
  );
}

export default function RouterView() {
  const { message } = App.useApp();
  const featureOn = useApp((s) => s.featureOn);
  const [cache, setCache] = useState<any>(null);
  const [router, setRouter] = useState<any>(null);
  const [costs, setCosts] = useState<any>(null);
  const [preview, setPreview] = useState<any>(null);
  const [previewTask, setPreviewTask] = useState('');

  const hasCache = featureOn('semantic_cache');
  const hasRouter = featureOn('model_router');
  const hasCosts = featureOn('cost_dashboard');

  const reload = () => {
    if (hasCache) apiGet('/cache').then(setCache).catch(() => {});
    if (hasRouter) apiGet('/router').then(setRouter).catch(() => {});
    if (hasCosts) apiGet('/costs').then(setCosts).catch(() => {});
  };
  useEffect(() => { reload(); }, [hasCache, hasRouter, hasCosts]);

  return (
    <Space direction="vertical" style={{ width: '100%' }} size="middle">
      <h3>🧭 智能路由与成本</h3>

      {/* 语义缓存 */}
      {!hasCache ? <UpgradeCard label="语义缓存（相似问题秒回，省 Token）" tier="专业版" /> : cache && (
        <Card size="small" title={<span>⚡ 语义缓存 {cache.advanced ? <Tag color="purple">企业版 · 高级</Tag> : <Tag color="blue">专业版 · 基础</Tag>}</span>}
          extra={<Space>
            <Switch size="small" checked={cache.enabled} onChange={async (v) => {
              setCache(await apiPost('/cache/config', { enabled: v }));
              message.info(v ? '语义缓存已开启' : '语义缓存已关闭');
            }} />
            <Button size="small" danger onClick={async () => {
              const r = await apiDelete('/cache');
              message.info(`已清空 ${r.cleared} 条缓存`);
              reload();
            }}>清空</Button>
          </Space>}>
          <Space size="large" wrap>
            <Statistic title="缓存条目" value={cache.entries} suffix={`/ ${cache.capacity}`} />
            <Statistic title="命中率" value={cache.hit_rate} suffix="%" />
            <Statistic title="命中次数" value={cache.hits} suffix={`/ ${cache.lookups} 次查询`} />
            <Statistic title="节省 Token" value={cache.saved_tokens} />
            <span>相似阈值：
              <InputNumber size="small" min={0.5} max={0.999} step={0.01} value={cache.threshold}
                onChange={async (v) => { if (v) setCache(await apiPost('/cache/config', { threshold: v })); }} />
            </span>
          </Space>
          <div className="hint-text" style={{ marginTop: 8 }}>
            对话模式下相似问题直接秒回历史答案（0 Token）。{cache.advanced ? '企业版：容量 2000 · TTL 7 天 · 节省额度计入成本仪表盘。' : `基础版：容量 ${cache.capacity} · TTL ${cache.ttl_hours}h。`}
          </div>
        </Card>
      )}

      {/* 模型路由 */}
      {!hasRouter ? <UpgradeCard label="模型智能路由（按任务复杂度分级选模型）" tier="专业版" /> : router && (
        <RouterConfig router={router} onChange={reload} />
      )}

      {/* 路由预演 */}
      {hasRouter && (
        <Card size="small" title="🧪 路由预演">
          <Space.Compact style={{ width: '100%' }}>
            <Input placeholder="输入任务文本，看看会被路由到哪一档…" value={previewTask}
              onChange={(e) => setPreviewTask(e.target.value)}
              onPressEnter={async () => setPreview(await apiPost('/router/preview', { task: previewTask }))} />
            <Button onClick={async () => setPreview(await apiPost('/router/preview', { task: previewTask }))}>预演</Button>
          </Space.Compact>
          {preview && (
            <div style={{ marginTop: 8, fontSize: '.86em' }}>
              复杂度得分：<b>{preview.score}</b> / 100
              {preview.selected
                ? <> → 命中档位 <Tag color="blue">{preview.selected.tier}</Tag> <code>{preview.selected.provider}/{preview.selected.model}</code></>
                : <span className="hint-text">（路由未启用或未配置档位 → 使用默认模型）</span>}
            </div>
          )}
        </Card>
      )}

      {/* 成本仪表盘 */}
      {!hasCosts ? <UpgradeCard label="成本仪表盘（模型成本 / 缓存节省分析）" tier="企业版" /> : costs && (
        <Card size="small" title="💰 成本仪表盘（企业版）" extra={<Button size="small" onClick={reload}>🔄 刷新</Button>}>
          <Space size="large" wrap style={{ marginBottom: 12 }}>
            <Statistic title="累计估算成本" value={costs.total_cost} prefix="¥" precision={4} />
            {costs.cache && <Statistic title="缓存节省 Token" value={costs.cache.saved_tokens} />}
          </Space>
          <Table size="small" rowKey={(r: any) => r.model} pagination={false}
            dataSource={Object.entries(costs.by_model || {}).map(([model, v]: any) => ({ model, ...v }))}
            columns={[
              { title: '模型', dataIndex: 'model' },
              { title: '任务数', dataIndex: 'tasks', width: 90 },
              { title: '输入 tk', dataIndex: 'prompt', width: 110, render: (v: number) => v.toLocaleString() },
              { title: '输出 tk', dataIndex: 'completion', width: 110, render: (v: number) => v.toLocaleString() },
              { title: '成本 ¥', dataIndex: 'cost', width: 100 },
            ]} />
          <div style={{ marginTop: 12 }}>
            <Text strong style={{ fontSize: '.86em' }}>近 30 天</Text>
            {Object.entries(costs.by_day || {}).map(([day, v]: any) => (
              <div key={day} style={{ display: 'flex', gap: 12, fontSize: '.8em', padding: '2px 0' }}>
                <span className="mono">{day}</span>
                <span>{v.tasks} 任务</span>
                <span>{v.tokens.toLocaleString()} tk</span>
                <span style={{ color: 'var(--yellow)' }}>¥{v.cost}</span>
              </div>
            ))}
          </div>
          <div className="hint-text" style={{ marginTop: 8 }}>{costs.note}</div>
        </Card>
      )}
    </Space>
  );
}

function RouterConfig({ router, onChange }: { router: any; onChange: () => void }) {
  const { message } = App.useApp();
  const [tiers, setTiers] = useState<any[]>(router.tiers?.length ? router.tiers : [
    { name: '轻量', provider: 'deepseek', model: 'deepseek-chat', max_score: 35 },
    { name: '强力', provider: 'deepseek', model: 'deepseek-reasoner', max_score: 100 },
  ]);
  const [providers, setProviders] = useState<any>(null);
  useEffect(() => { apiGet('/providers').then(setProviders).catch(() => {}); }, []);
  const maxTiers = router.max_tiers;
  const allP = providers ? [...(providers.cloud || []), ...(providers.local || []), ...(providers.custom || [])] : [];

  const save = async (enabled?: boolean) => {
    const r = await apiPost('/router', { tiers, ...(enabled !== undefined ? { enabled } : {}) });
    if (r.error) { message.error(r.error); return; }
    message.success('路由配置已保存');
    onChange();
  };

  return (
    <Card size="small"
      title={<span>🧭 模型智能路由 {maxTiers == null ? <Tag color="purple">企业版 · N 级</Tag> : <Tag color="blue">专业版 · {maxTiers} 级</Tag>}</span>}
      extra={<Switch size="small" checkedChildren="启用" unCheckedChildren="停用" checked={router.enabled}
        onChange={(v) => save(v)} />}>
      <Paragraph type="secondary" style={{ fontSize: '.8em' }}>
        按任务复杂度（0~100 启发式打分）自动选择模型档位：简单问答走便宜小模型、复杂任务走强模型。
        档位按 max_score 升序命中第一个满足 score ≤ max_score 的档。
      </Paragraph>
      {tiers.map((t, i) => (
        <Space key={i} style={{ marginBottom: 6 }} wrap>
          <Input size="small" style={{ width: 90 }} placeholder="档位名" value={t.name}
            onChange={(e) => setTiers(tiers.map((x, k) => (k === i ? { ...x, name: e.target.value } : x)))} />
          <Select size="small" style={{ width: 120 }} value={t.provider} showSearch
            options={allP.map((p: string) => ({ value: p, label: providers?.labels?.[p] || p }))}
            onChange={(v) => setTiers(tiers.map((x, k) => (k === i ? { ...x, provider: v } : x)))} />
          <Input size="small" style={{ width: 200 }} placeholder="模型名" value={t.model}
            onChange={(e) => setTiers(tiers.map((x, k) => (k === i ? { ...x, model: e.target.value } : x)))} />
          <span className="hint-text">复杂度 ≤</span>
          <InputNumber size="small" min={1} max={100} value={t.max_score}
            onChange={(v) => setTiers(tiers.map((x, k) => (k === i ? { ...x, max_score: v || 100 } : x)))} />
          {tiers.length > 1 && (
            <Button size="small" type="text" danger onClick={() => setTiers(tiers.filter((_, k) => k !== i))}>✕</Button>
          )}
        </Space>
      ))}
      <Space style={{ marginTop: 6 }}>
        <Button size="small" disabled={maxTiers != null && tiers.length >= maxTiers}
          onClick={() => setTiers([...tiers, { name: `档位${tiers.length + 1}`, provider: allP[0] || 'openai', model: '', max_score: 100 }])}>
          ➕ 加一档{maxTiers != null && tiers.length >= maxTiers ? `（专业版最多 ${maxTiers} 级，企业版不限）` : ''}
        </Button>
        <Button size="small" type="primary" onClick={() => save()}>保存档位</Button>
      </Space>
      {Object.keys(router.stats || {}).length > 0 && (
        <div className="hint-text" style={{ marginTop: 8 }}>
          路由统计：{Object.entries(router.stats).map(([k, v]: any) => `${k} ×${v}`).join(' · ')}
        </div>
      )}
    </Card>
  );
}
