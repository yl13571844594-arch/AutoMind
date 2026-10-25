// v1.11.15 输入框模型选择器（对话输入框工具栏的 🧠 胶囊）。
//
// 三个选项来源：
//   1. 默认 —— 跟随全局默认 / 各模式模型（服务端原行为，含专业版默认自动路由）；
//   2. 使用配置好的模型 —— GET /api/config/available-models 返回的清单，
//      按 provider 分组，**只列已配置 API Key（或本地 ollama）的模型**；
//   3. 自动路由 —— 专业版/企业版特性（model_router）。社区版置灰 🔒，
//      点击说明原因并打开升级面板（license）。
//
// 选择是**常驻**的：存在 localStorage（store/modelChoice），之后每条消息都带；
// 与「＋ 菜单 → 本条模式」那种一次性 overrides 不同。
import { App, type MenuProps } from 'antd';
import { useEffect, useRef } from 'react';
import {
  choiceLabel, shortModel, sourceHint, type ModelChoice, type ModelOption,
  type ModelProviderRow,
} from '../../lib/modelChoice';
import { EDITION_LABELS, useApp } from '../../store/app';
import { useModelChoice } from '../../store/modelChoice';
import { useUi } from '../../store/ui';
import SafeDropdown from '../ui/SafeDropdown';
import './ModelPicker.css';

/** 两行菜单条目（复用 global.css 的 .pm-* 样式，与 ＋ 菜单观感一致）。 */
function pm(icon: string, title: React.ReactNode, desc?: React.ReactNode, on?: boolean,
  locked?: boolean) {
  return (
    <span className={'pm-item' + (on ? ' on' : '')}>
      <span className="pm-ico">{icon}</span>
      <span className="pm-text">
        <span className="pm-title">{title}</span>
        {desc && <span className="pm-desc">{desc}</span>}
      </span>
      {on && <span className="pm-check">✓</span>}
    </span>
  );
}

function sub(icon: string, title: React.ReactNode, desc?: React.ReactNode, on?: boolean) {
  return (
    <span className={'pm-sub' + (on ? ' on' : '')}>
      <span className="pm-ico">{icon}</span>
      <span className="pm-text">
        <span className="pm-title">{title}</span>
        {desc && <span className="pm-desc">{desc}</span>}
      </span>
      {on && <span className="pm-check">✓</span>}
    </span>
  );
}

export default function ModelPicker() {
  const { message } = App.useApp();
  const choice = useModelChoice((s) => s.choice);
  const available = useModelChoice((s) => s.available);
  const loading = useModelChoice((s) => s.loading);
  const error = useModelChoice((s) => s.error);
  const load = useModelChoice((s) => s.load);
  const setChoice = useModelChoice((s) => s.setChoice);
  const status = useApp((s) => s.status);
  const edition = useApp((s) => s.edition);
  const prevEdition = useRef(edition);

  // 首次挂载拉一次清单；应用内激活/升级许可证后版本变化 → 立刻刷新自动路由可用性
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (prevEdition.current === edition) return;
    prevEdition.current = edition;
    void load(true);
  }, [edition, load]);

  const auto = available?.auto;
  // pill 上的"默认 · xxx"用当前生效模型（status.model 会反映各模式单独配置的模型），
  // 菜单里的"默认模型"说明用全局默认（available.default 来自持久化配置）
  const defaultModel = status?.model || available?.default?.model || '';
  const globalDefault = available?.default?.model || status?.model || '';
  // 当前选择是否可用（只用于显示 ⚠ 提示；**不**静默替用户改选择 —— 服务端会在
  // 回执里如实说"本条没生效、继续用默认模型"）
  const manualInvalid = choice.mode === 'manual' && !!available
    && !(available.options || []).some((o) => o.provider === choice.provider
      && o.model === choice.model && o.ready);
  const autoUnavailable = choice.mode === 'auto' && !!auto && !auto.available;
  const needWarn = manualInvalid || autoUnavailable || (!available && !!error);

  const title = (() => {
    if (!available && error) return `模型列表加载失败：${error}（点击可重试）`;
    if (choice.mode === 'manual') {
      return `本条消息使用 ${choice.provider}/${choice.model}`
        + (manualInvalid ? '（当前不可用：未配置 Key 或模型已被移除，发送后会收到未生效回执）' : '，点击更换');
    }
    if (choice.mode === 'auto') {
      if (!auto) return '自动路由：按任务复杂度自动切换模型档位';
      if (!auto.available) return auto.reason || '自动路由需要专业版/企业版';
      if (!auto.enabled) return '自动路由：路由未启用，本条将使用当前默认模型';
      return `自动路由：按任务复杂度在档位间自动选择${auto.summary ? `（${auto.summary}）` : ''}`;
    }
    return `跟随默认模型${defaultModel ? `：${defaultModel}` : ''}；点击为本条消息指定模型`;
  })();

  const chooseAuto = () => {
    const info = useModelChoice.getState().available?.auto;
    if (!info) { void load(true); return; }
    if (!info.available) {
      message.info('自动路由是专业版/企业版功能：按任务复杂度自动切换模型档位');
      useUi.getState().openModal('license');
      return;
    }
    setChoice({ mode: 'auto' });
    message.success(!info.enabled || !info.tiers.length
      ? '已选择自动路由：路由未启用时本条将使用当前默认模型'
      : `已选择自动路由：${info.summary}`, 3);
  };

  const autoDesc = (() => {
    if (!available) return loading ? '正在加载可用模型…' : '按任务复杂度自动切换模型档位';
    if (!auto) return '';
    if (!auto.available) return '需要专业版/企业版 · 点击了解升级';
    if (!auto.enabled) return '路由未启用（可在「模型路由」中开启）';
    if (!auto.tiers.length) return '尚未配置路由档位';
    return `按任务复杂度自动选择${auto.summary ? `：${auto.summary}` : ''}`;
  })();
  const autoItem = {
    key: 'mp:auto',
    disabled: loading && !available,
    label: (
      <span className={'pm-item' + (choice.mode === 'auto' ? ' mp-auto-on' : '')
        + (!available || !auto?.available ? ' mp-locked' : '')}>
        <span className="pm-ico">✨</span>
        <span className="pm-text">
          <span className="pm-title">
            自动路由
            {auto?.available && auto.enabled && auto.summary
              ? <span className="pm-count">{auto.summary}</span> : null}
            {auto && !auto.available ? ' 🔒' : null}
          </span>
          <span className="pm-desc">{autoDesc}</span>
        </span>
        {choice.mode === 'auto' && <span className="pm-check">✓</span>}
      </span>
    ),
    onClick: chooseAuto,
  };

  const defaultItem = {
    key: 'mp:default',
    label: sub('🧠', '默认模型',
      globalDefault ? `跟随全局设置 · ${globalDefault}` : '跟随全局设置 / 各模式模型',
      choice.mode === 'default'),
    onClick: () => setChoice({ mode: 'default' }),
  };

  // 按 provider 分组渲染选项（顺序由服务端给出：当前 / 默认提供商在前）
  const byProvider = new Map<string, ModelOption[]>();
  for (const o of available?.options || []) {
    const list = byProvider.get(o.provider) || [];
    list.push(o);
    byProvider.set(o.provider, list);
  }
  const providerGroups: NonNullable<MenuProps['items']> = (available?.providers || []).map(
    (row: ModelProviderRow) => {
      const opts = byProvider.get(row.provider) || [];
      let children: NonNullable<MenuProps['items']>;
      if (!row.ready) {
        children = [{
          key: `mp:missing:${row.provider}`, disabled: true,
          label: sub('🔑', '未配置 API Key', '在「模型配置」里填写该提供商的 Key 后刷新'),
        }];
      } else if (!opts.length) {
        children = [{
          key: `mp:empty:${row.provider}`, disabled: true,
          label: sub('📭', '没有可选模型', '可在「模型配置」中添加自定义模型'),
        }];
      } else {
        children = opts.map((o) => {
          const on = choice.mode === 'manual' && choice.provider === o.provider
            && choice.model === o.model;
          return {
            key: `mp:m:${o.key}`,
            disabled: !o.ready,
            label: sub(
              o.source === 'custom' ? '⭐' : '🧠',
              o.model,
              [sourceHint(o.source), o.ready ? '' : '未配置 API Key']
                .filter(Boolean).join(' · '),
              on),
            title: `${o.provider_label || o.provider} / ${o.model}`,
            onClick: () => setChoice({ mode: 'manual', provider: o.provider, model: o.model }),
          };
        });
      }
      return { type: 'group' as const, label: row.label, children };
    });

  const items: MenuProps['items'] = [
    ...(loading && !available
      ? [{ key: 'mp:loading', disabled: true, label: sub('⏳', '正在加载可用模型…') }]
      : []),
    ...(!available && error
      ? [{
        key: 'mp:error', label: sub('⚠️', '模型列表加载失败', `点击重试 · ${error}`),
        onClick: () => { void load(true); },
      }]
      : []),
    { type: 'group', label: '自动路由', children: [autoItem] },
    { type: 'group', label: '使用配置好的模型', children: [defaultItem] },
    ...providerGroups,
    { type: 'divider' },
    ...(available && error
      ? [{
        key: 'mp:stale', label: sub('⚠️', '刷新失败，显示的是上次的清单', '点击重试'),
        onClick: () => { void load(true); },
      }]
      : []),
    {
      key: 'mp:refresh', label: sub('↻', '刷新模型列表', '配置了新的 Key / 模型后点这里'),
      onClick: () => { void load(true); message.info('正在刷新模型列表…'); },
    },
    {
      key: 'mp:config', label: pm('⚙', '模型配置…', '提供商 / API Key / 模型路由档位'),
      onClick: () => useUi.getState().openModal('model'),
    },
  ];

  const label = choiceLabel(choice as ModelChoice, defaultModel);
  const editionLabel = auto?.available
    ? (auto.edition_label || EDITION_LABELS[auto.edition] || auto.edition) : '';

  return (
    <SafeDropdown menu={{ items }} placement="topLeft" overlayClassName="model-picker-menu"
      onOpenChange={(o) => { if (o) void load(); }}>
      <button type="button" aria-label="选择本条消息使用的模型"
        className={'model-pill' + (choice.mode === 'auto' ? ' auto' : '')
          + (needWarn ? ' warn' : '')}
        title={title}>
        {choice.mode === 'auto' && <span className="model-breath" aria-hidden="true" />}
        <span className="model-pill-label">{label}</span>
        {choice.mode === 'auto' && editionLabel && (
          <span className="model-pill-tag">{editionLabel}</span>
        )}
        {needWarn && <span className="model-warn" aria-hidden="true">⚠</span>}
        <span className="model-caret" aria-hidden="true">▴</span>
      </button>
    </SafeDropdown>
  );
}

/** 供别处（如未来统计/展示）复用的短名格式化。 */
export { shortModel };
