// v1.11.15 输入框模型选择：类型 / 本地持久化 / 发送协议的增量字段。
//
// 与"本条一次性设置"不同，模型选择是**常驻**的：选了一次之后每条消息都带上，
// 直到用户改回"默认"。所以它不进 lib/overrides.ts 的 EMPTY_OVERRIDES 重置流程，
// 而是由 store/modelChoice 持有、发送时并入 overrides。
//
// 服务端协议（见 automind/core/model_choice.py）：
//   overrides.model_mode = "default" | "manual" | "auto"
//   overrides.provider / overrides.model   —— 仅 manual 时携带
import { apiGet } from '../api/client';

export type ModelMode = 'default' | 'manual' | 'auto';

/** 用户当前的选择（持久化在 localStorage）。 */
export interface ModelChoice {
  mode: ModelMode;
  provider?: string;
  model?: string;
}

export interface ModelOption {
  key: string;
  provider: string;
  provider_label?: string;
  model: string;
  label?: string;
  /** false = 没配 API Key（后端通常不返回这类；保留给"default 指向未配置的提供商"场景） */
  ready: boolean;
  /** 来源：custom / current / default / mode / provider / recommended */
  source: string;
}

export interface ModelProviderRow {
  provider: string;
  label: string;
  ready: boolean;
  models: string[];
}

export interface ModelTier {
  name: string;
  provider: string;
  model: string;
  max_score?: number;
}

export interface AutoRouterInfo {
  available: boolean;
  edition: string;
  edition_label?: string;
  enabled: boolean;
  tiers: ModelTier[];
  /** 档位摘要，如 "轻量 → 强力" */
  summary: string;
  max_tiers?: number | null;
  /** available=false 时的升级原因（社区版） */
  reason: string;
}

export interface AvailableModels {
  default: { provider: string; model: string };
  current: { provider: string; model: string };
  options: ModelOption[];
  providers: ModelProviderRow[];
  auto: AutoRouterInfo;
}

export const MODEL_CHOICE_KEY = 'automind_model_choice';

export const DEFAULT_CHOICE: ModelChoice = { mode: 'default' };

function asStr(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

/**
 * 校验并规范化本地持久化的选择。
 *
 * 脏数据（手改 localStorage、旧版本残留、字段缺失）一律回退 default ——
 * 把无效的 provider/model 发给服务端只会每条消息都收到一条失败回执。
 */
export function normalizeChoice(raw: unknown): ModelChoice {
  if (!raw || typeof raw !== 'object') return { ...DEFAULT_CHOICE };
  const o = raw as Record<string, unknown>;
  const mode = asStr(o.mode);
  if (mode === 'auto') return { mode: 'auto' };
  if (mode === 'manual') {
    const provider = asStr(o.provider).trim();
    const model = asStr(o.model).trim();
    return provider && model ? { mode: 'manual', provider, model } : { ...DEFAULT_CHOICE };
  }
  return { ...DEFAULT_CHOICE };
}

export function loadChoice(): ModelChoice {
  try {
    return normalizeChoice(JSON.parse(localStorage.getItem(MODEL_CHOICE_KEY) || 'null'));
  } catch {
    return { ...DEFAULT_CHOICE };
  }
}

export function saveChoice(c: ModelChoice): void {
  try {
    localStorage.setItem(MODEL_CHOICE_KEY, JSON.stringify(normalizeChoice(c)));
  } catch { /* 隐私模式 / 配额满：选择仍在本会话内有效 */ }
}

/** 拉取服务端的可用模型清单。 */
export function fetchAvailableModels(): Promise<AvailableModels> {
  return apiGet('/config/available-models');
}

/** 对服务端返回值做最低限度的结构校验（旧版服务端 / 代理返回 HTML 时不至于崩）。 */
export function isValidAvailable(d: any): d is AvailableModels {
  return !!d && typeof d === 'object'
    && Array.isArray(d.options)
    && Array.isArray(d.providers)
    && !!d.auto && typeof d.auto === 'object'
    && typeof d.auto.available === 'boolean';
}

/**
 * 发送时并入 overrides 的模型增量。
 *
 * default 返回 undefined（**不带** model_mode，服务端保持原有的模式模型 +
 * 默认自动路由行为）；manual 必须 provider/model 齐全，否则同样当作默认。
 */
export function wireModelOverrides(c: ModelChoice | undefined): Record<string, any> | undefined {
  if (!c || c.mode === 'default') return undefined;
  if (c.mode === 'auto') return { model_mode: 'auto' };
  if (!c.provider || !c.model) return undefined;
  return { model_mode: 'manual', provider: c.provider, model: c.model };
}

/** 模型短名：只留 model 本身（下拉/气泡里不重复展示 provider），过长截断。 */
export function shortModel(model: string | undefined | null, max = 22): string {
  const m = (model || '').trim();
  if (!m) return '';
  return m.length > max ? m.slice(0, max - 1) + '…' : m;
}

/** 输入框 pill 的文案：默认 → "🧠 默认 · xxx"；手动 → "🧠 xxx"；自动 → "🧠 自动路由"。 */
export function choiceLabel(c: ModelChoice, defaultModel?: string): string {
  if (c.mode === 'auto') return '🧠 自动路由';
  if (c.mode === 'manual') return `🧠 ${shortModel(c.model)}`;
  const m = shortModel(defaultModel);
  return m ? `🧠 默认 · ${m}` : '🧠 默认模型';
}

/** 模型来源的中文说明（下拉菜单第二行）。 */
export function sourceHint(source: string): string {
  return ({
    custom: '自定义收藏',
    current: '当前使用',
    default: '默认模型',
    mode: '模式模型',
    provider: '提供商配置',
    recommended: '推荐模型',
  } as Record<string, string>)[source] || '';
}
