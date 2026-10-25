// 🧭 智能路由与成本（v1.11.15 视觉重做）
//
// 三块能力、三种权限状态：
//   · 语义缓存（专业版）—— 环形命中率仪表 + 容量条 + 阈值滑杆 + 清空
//   · 模型智能路由（专业 2 级 / 企业 N 级）—— 档位阶梯 + 可编辑档位 + 路由统计
//   · 成本仪表盘（企业版）—— 30 天 SVG 柱状图 + 按模型占比表 + 缓存节省 + 口径说明
// 社区/专业缺少的能力统一渲染成渐变"能力卡"（说明收益 + 打开升级面板），
// 不留空白、也不把未解锁的数据伪装成 0。
//
// 数据只来自现有接口：GET /cache（POST /cache/config、DELETE /cache）、
// GET/POST /router、POST /router/preview、GET /costs。不新增后端接口、不引图表依赖
// （趋势/圆环/柱状图全部手写内联 SVG）。
import {
  App, Button, Input, InputNumber, Skeleton, Slider, Switch, Table,
  type TableColumnsType,
} from 'antd';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { apiDelete, apiGet, apiPost } from '../../api/client';
import {
  asArray, asRecord, clampNum, fixed, pctText, str, toBool, toInt, toNum,
} from '../../lib/num';
import { useAsync } from '../../lib/useAsync';
import { AsyncBoundary } from '../ui/AsyncBoundary';
import { ErrorBoundary } from '../ui/ErrorBoundary';
import Select from '../ui/SafeSelect';
import { EmptyState } from '../ui/Panel';
import { EDITION_LABELS, useApp } from '../../store/app';
import { useUi } from '../../store/ui';
// @ts-ignore 本项目未引入 vite/client 类型声明；CSS 由 Vite 在构建期打包（组件内 import）
import './RouterView.css';

// ══════════════════════════════════════════════════════════════
// 类型（与 pro/automind_pro/model_router.py 的返回结构对齐，旧字段全部保留）
// ══════════════════════════════════════════════════════════════

interface CacheStats {
  enabled: boolean; advanced?: boolean; entries: number; capacity: number;
  ttl_hours?: number; threshold: number; lookups: number; hits: number;
  hit_rate: number; saved_tokens: number;
  /** 服务端是否上报了合法的 similar-threshold（false 时滑杆不可用，按默认值展示） */
  threshold_valid?: boolean;
  /** 数据不完整时的降级原因（缺字段/类型错误/接口异常兜底），用于界面提示 */
  _degraded?: string[];
}

interface RouterTier { name: string; provider: string; model: string; max_score: number }
interface RouterCfg {
  enabled: boolean; tiers: RouterTier[]; max_tiers: number | null;
  stats: Record<string, number>;
  _degraded?: string[];
}

interface DayCost { tasks: number; tokens: number; prompt?: number; completion?: number; cost: number }
interface ModelCost { tasks: number; prompt: number; completion: number; cost: number; share?: number }
interface CacheCost extends Partial<CacheStats> {
  saved_cost?: number; saved_cost_note?: string;
}
interface CostsData {
  total_cost: number; total_tokens?: number; total_tasks?: number;
  by_model: Record<string, ModelCost>; by_day: Record<string, DayCost>;
  cache: CacheCost | null; note: string; currency?: string;
  range_days?: number; generated_at?: string; top_model?: string | null;
  _degraded?: string[];
}

interface Providers { cloud?: string[]; local?: string[]; custom?: string[]; labels?: Record<string, string> }
interface PreviewResp {
  score: number;
  selected: null | { provider: string; model: string; tier: string; score: number };
}

type Tone = 'accent' | 'purple' | 'green' | 'yellow' | 'red';
const TONE_CLASS: Tone[] = ['accent', 'purple', 'green', 'yellow', 'red'];
const DEFAULT_TIERS: RouterTier[] = [
  { name: '轻量', provider: 'deepseek', model: 'deepseek-chat', max_score: 35 },
  { name: '强力', provider: 'deepseek', model: 'deepseek-reasoner', max_score: 100 },
];
/** 服务端未上报/上报了非法阈值时的默认相似阈值（与 pro/automind_pro/semantic_cache 一致） */
const DEFAULT_THRESHOLD = 0.93;
/** 成本口径的兜底文案（服务端缺 note 时展示，避免出现 undefined） */
const DEFAULT_NOTE = '按内置单价表估算（¥/百万 token），仅供参考；不在单价表中的模型成本按 0 计';
const PREVIEW_SAMPLES = [
  '帮我把这段会议记录总结成三条要点',
  '写一个抓取天气 API、带重试的 Python 脚本',
  '分三步重构这个模块，然后补单元测试并输出报告',
];

// ══════════════════════════════════════════════════════════════
// 数据防御：三个接口的响应先过 sanitize，再进入渲染
//
// 为什么必须做：这些接口由不同版本的商业扩展包提供，字段可能缺失 / null /
// 是字符串数字 / 被门控响应替换。历史上 `thr.toFixed(2)`（cache.threshold
// 缺失）就是一处未兜底调用，直接让整页白屏（v1.11.16 修复）。这里统一把
// 原始响应转成"字段齐全、类型正确、数值有限"的对象，并记录降级原因。
// ══════════════════════════════════════════════════════════════

/** 版本门控 / 服务端错误被包成 200 时的显式失败：宁可显示错误条，也不静默显示 0。 */
function guardPayload(raw: unknown): unknown {
  const r = asRecord(raw);
  const feature = str(r.feature);
  if (feature) {
    // 403 降级路由的响应体带 feature。若 /api/status 已把该特性标记为已授权，
    // 那就不是"没开通"，而是服务端没挂载真实路由（旧扩展包没注入该特性 /
    // 热激活未完成 / 主程序与扩展包版本不一致）。此时继续说"未开通或版本
    // 门控"自相矛盾，用户也不知道该做什么 —— 直接指向重启 / 换扩展包（v1.11.17）。
    const authorized = useApp.getState().featureOn(feature);
    throw new Error(authorized
      ? `服务端未挂载该特性（feature=${feature}）：/api/status 显示已授权，但请求落到了 403 降级路由。`
        + '通常是扩展包版本过旧或热激活未完成，请重启服务；仍不行请更新到与主程序同版本的扩展包'
      : `该功能未开通或被版本门控（feature=${feature}），本卡片已降级`);
  }
  const error = str(r.error);
  if (error) throw new Error(error);
  return raw;
}

/** 合法的相似阈值：有限数字且在 [0.5, 0.999]；否则回默认 0.93。 */
function safeThreshold(v: unknown): number {
  const n = toNum(v, NaN);
  return Number.isFinite(n) && n >= 0.5 && n <= 0.999 ? n : DEFAULT_THRESHOLD;
}

function sanitizeCache(raw: unknown): CacheStats {
  const r = asRecord(raw);
  const degraded: string[] = [];
  const lookups = Math.max(0, toNum(r.lookups));
  const hits = Math.max(0, toNum(r.hits));
  if (r.lookups == null || r.hits == null) degraded.push('缺少命中/查询计数');
  const hitRateGiven = r.hit_rate != null;
  const hitRateNum = toNum(r.hit_rate, NaN);
  const hitRate = clampNum(
    hitRateGiven ? hitRateNum : (lookups > 0 ? (hits / lookups) * 100 : 0),
    0, 100, 0,
  );
  if (!hitRateGiven) degraded.push('缺少 hit_rate（按 hits/lookups 折算）');
  else if (!Number.isFinite(hitRateNum)) degraded.push('hit_rate 非法（按 hits/lookups 折算）');
  const thresholdGiven = r.threshold != null;
  const threshold = safeThreshold(r.threshold);
  if (thresholdGiven && threshold === DEFAULT_THRESHOLD
      && toNum(r.threshold, NaN) !== DEFAULT_THRESHOLD) {
    degraded.push('similarity threshold 非法（按默认 0.93 展示）');
  } else if (!thresholdGiven) {
    degraded.push('缺少 similarity threshold（按默认 0.93 展示）');
  }
  const ttl = toNum(r.ttl_hours, NaN);
  const entries = Math.max(0, toNum(r.entries));
  const capacity = Math.max(0, toNum(r.capacity));
  return {
    enabled: toBool(r.enabled),
    advanced: toBool(r.advanced),
    entries,
    capacity,
    ttl_hours: Number.isFinite(ttl) && ttl > 0 ? ttl : undefined,
    threshold,
    threshold_valid: thresholdGiven && safeThreshold(r.threshold) === toNum(r.threshold, NaN),
    lookups,
    hits,
    hit_rate: hitRate,
    saved_tokens: Math.max(0, toNum(r.saved_tokens)),
    _degraded: degraded,
  };
}

function sanitizeRouter(raw: unknown): RouterCfg {
  const r = asRecord(raw);
  const degraded: string[] = [];
  if (typeof r.enabled !== 'boolean') {
    degraded.push(`enabled 字段类型异常（已按${toBool(r.enabled) ? '启用' : '未启用'}解析）`);
  }
  const tiersRaw = asArray(r.tiers);
  const tiers: RouterTier[] = [];
  for (const t of tiersRaw) {
    const o = asRecord(t);
    const provider = str(o.provider).trim();
    const model = str(o.model).trim();
    if (!provider || !model) continue;              // 坏档位直接忽略，不能让 Input/Select 崩
    tiers.push({
      name: str(o.name, `档位${tiers.length + 1}`).slice(0, 20) || `档位${tiers.length + 1}`,
      provider,
      model,
      max_score: clampNum(o.max_score, 1, 100, 100),
    });
  }
  if (tiers.length !== tiersRaw.length) {
    degraded.push(`${tiersRaw.length - tiers.length} 个档位缺少 provider/model 已忽略`);
  }
  const stats: Record<string, number> = {};
  for (const [k, v] of Object.entries(asRecord(r.stats))) {
    const n = toNum(v, NaN);
    if (Number.isFinite(n) && n >= 0) stats[k] = Math.trunc(n);
  }
  const mt = toNum(r.max_tiers, NaN);
  return {
    enabled: toBool(r.enabled),
    tiers,
    max_tiers: Number.isFinite(mt) && mt >= 1 ? Math.trunc(mt) : null,
    stats,
    _degraded: degraded,
  };
}

function sanitizeCosts(raw: unknown): CostsData {
  const r = asRecord(raw);
  const degraded: string[] = [];
  if (raw == null || typeof raw !== 'object' || Array.isArray(raw)) {
    degraded.push('响应不是对象（按空数据展示）');
  }
  const byModelRaw = asRecord(r.by_model);
  const byModel: Record<string, ModelCost> = {};
  for (const [model, v] of Object.entries(byModelRaw)) {
    const o = asRecord(v);
    if (!model.trim()) continue;
    const cost = Math.max(0, toNum(o.cost));
    byModel[model] = {
      tasks: Math.max(0, toInt(o.tasks)),
      prompt: Math.max(0, toInt(o.prompt)),
      completion: Math.max(0, toInt(o.completion)),
      cost,
      share: clampNum(o.share, 0, 1, 0),
    };
  }
  const byDayRaw = asRecord(r.by_day);
  const byDay: Record<string, DayCost> = {};
  for (const [day, v] of Object.entries(byDayRaw)) {
    const o = asRecord(v);
    if (!day.trim()) continue;
    byDay[day] = {
      tasks: Math.max(0, toInt(o.tasks)),
      tokens: Math.max(0, toInt(o.tokens)),
      prompt: Math.max(0, toInt(o.prompt)),
      completion: Math.max(0, toInt(o.completion)),
      cost: Math.max(0, toNum(o.cost)),
    };
  }
  // 总额缺失时从明细回算（旧服务端没有 total_tokens/total_tasks）
  const modelCostSum = Object.values(byModel).reduce((a, m) => a + m.cost, 0);
  const dayCostSum = Object.values(byDay).reduce((a, d) => a + d.cost, 0);
  const totalCostRaw = toNum(r.total_cost, NaN);
  const total_cost = Math.max(0, Number.isFinite(totalCostRaw) ? totalCostRaw : (modelCostSum || dayCostSum));
  const totalTokRaw = toNum(r.total_tokens, NaN);
  const total_tokens = Math.max(0, Number.isFinite(totalTokRaw)
    ? totalTokRaw
    : Object.values(byDay).reduce((a, d) => a + d.tokens, 0));
  const totalTasksRaw = toNum(r.total_tasks, NaN);
  const total_tasks = Math.max(0, Math.trunc(Number.isFinite(totalTasksRaw)
    ? totalTasksRaw
    : Object.values(byModel).reduce((a, m) => a + m.tasks, 0)));
  // share 缺失/全 0 时用总额折算，占比条不至于全是 0（后端正常时会给出 sum≈1 的 share）
  const shareSum = Object.values(byModel).reduce((a, m) => a + (m.share || 0), 0);
  if (shareSum <= 0 && total_cost > 0) {
    for (const m of Object.values(byModel)) {
      m.share = Math.round((m.cost / total_cost) * 1000) / 1000;
    }
  }
  for (const m of Object.values(byModel)) {
    if (m.share == null || !Number.isFinite(m.share)) m.share = 0;
  }
  const cacheRaw = r.cache;
  let cache: CacheCost | null = null;
  if (cacheRaw != null && typeof cacheRaw === 'object' && !Array.isArray(cacheRaw)) {
    const c = asRecord(cacheRaw);
    const savedTok = toNum(c.saved_tokens, NaN);
    const savedCost = toNum(c.saved_cost, NaN);
    cache = {
      enabled: toBool(c.enabled),
      advanced: toBool(c.advanced),
      entries: Math.max(0, toNum(c.entries)),
      capacity: Math.max(0, toNum(c.capacity)),
      lookups: Math.max(0, toNum(c.lookups)),
      hits: Math.max(0, toNum(c.hits)),
      hit_rate: clampNum(c.hit_rate, 0, 100, 0),
      saved_tokens: Number.isFinite(savedTok) ? Math.max(0, savedTok) : undefined,
      saved_cost: Number.isFinite(savedCost) ? Math.max(0, savedCost) : undefined,
      saved_cost_note: str(c.saved_cost_note) || undefined,
    };
  } else if (cacheRaw != null) {
    degraded.push('cache 字段不是对象（已忽略）');
  }
  let topModel = str(r.top_model).trim();
  if (!topModel || !byModel[topModel]) {
    const best = Object.entries(byModel).sort((a, b) => b[1].cost - a[1].cost)[0];
    topModel = best ? best[0] : '';
  }
  const rangeRaw = toNum(r.range_days, NaN);
  return {
    total_cost,
    total_tokens,
    total_tasks,
    by_model: byModel,
    by_day: byDay,
    cache,
    note: str(r.note) || DEFAULT_NOTE,
    currency: str(r.currency) || 'CNY',
    range_days: Number.isFinite(rangeRaw) && rangeRaw >= 1 && rangeRaw <= 365
      ? Math.trunc(rangeRaw) : 30,
    generated_at: str(r.generated_at) || undefined,
    top_model: topModel || null,
    _degraded: degraded,
  };
}

function sanitizeProviders(raw: unknown): Providers {
  const r = asRecord(raw);
  const list = (v: unknown) => asArray(v).map((x) => str(x).trim()).filter(Boolean);
  const labels: Record<string, string> = {};
  for (const [k, v] of Object.entries(asRecord(r.labels))) {
    // 只接受字符串标签：数字/对象当标签会渲染出 "123" 这种莫名奇妙的供应商名
    const s = typeof v === 'string' ? v.trim() : '';
    if (k.trim() && s) labels[k] = s;
  }
  return {
    cloud: list(r.cloud),
    local: list(r.local),
    custom: list(r.custom),
    labels,
  };
}

function sanitizePreview(raw: unknown): PreviewResp {
  const r = asRecord(raw);
  const selRaw = asRecord(r.selected);
  const provider = str(selRaw.provider).trim();
  const model = str(selRaw.model).trim();
  const selected = provider || model
    ? {
      provider,
      model,
      tier: str(selRaw.tier, '未命名档位'),
      score: clampNum(selRaw.score, 0, 100, 0),
    }
    : null;
  return { score: clampNum(r.score, 0, 100, 0), selected };
}

// ══════════════════════════════════════════════════════════════
// 小工具（格式化只走 Number.isFinite，见 web/src/lib/num.ts）
// ══════════════════════════════════════════════════════════════

const clamp = (n: unknown, lo = 0, hi = 100): number => clampNum(n, lo, hi, lo);

function fmtMoney(n: unknown): string {
  const v = toNum(n);
  if (Math.abs(v) >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (Math.abs(v) >= 1) return v.toFixed(2);
  if (v !== 0) return v.toFixed(4);
  return '0.00';
}

function fmtTokens(n: unknown): string {
  const v = Math.abs(toNum(n));
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (v >= 1e4) return `${(v / 1e3).toFixed(1)}K`;
  return Math.round(v).toLocaleString();
}

/** 图表坐标轴用：金额刻度要短（¥0.50 而不是 ¥0.5000），避免轴标签比图还长。 */
function fmtMoneyAxis(v: unknown): string {
  const n = Math.abs(toNum(v));
  if (n === 0) return '0';
  if (n >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (n >= 10) return n.toFixed(0);
  return n.toFixed(2);
}

const fmtInt = (n: unknown) => Math.round(toNum(n)).toLocaleString();

function dayKey(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${d.getFullYear()}-${m}-${day}`;
}

/** 最近 n 个自然日（含今天）的日期键，用于补齐 by_day 里没有任务的空白天。 */
function lastDays(n: number): string[] {
  const out: string[] = [];
  const today = new Date();
  const count = Number.isFinite(n) ? Math.max(1, Math.min(365, Math.trunc(n))) : 30;
  for (let i = count - 1; i >= 0; i--) {
    out.push(dayKey(new Date(today.getFullYear(), today.getMonth(), today.getDate() - i)));
  }
  return out;
}

const shortDay = (key: string) => String(key).slice(5);

function fmtTime(iso?: unknown): string {
  const s = str(iso);
  if (!s) return '—';
  try {
    const d = new Date(s);
    if (Number.isNaN(d.getTime())) return s;
    return d.toLocaleString(undefined, { hour12: false });
  } catch {
    return s;
  }
}

// ══════════════════════════════════════════════════════════════
// 图表构件（纯内联 SVG，无第三方图表库）
// ══════════════════════════════════════════════════════════════

/** 环形仪表：用于缓存命中率 / 预演复杂度。 */
function Ring({ pct, size = 132, stroke = 11, tone = 'accent', center, sub }: {
  pct: number; size?: number; stroke?: number; tone?: Tone;
  center?: ReactNode; sub?: ReactNode;
}) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const v = clamp(pct);
  return (
    <div className="rv-ring" style={{ width: size, height: size }}>
      <svg viewBox={`0 0 ${size} ${size}`} className="rv-ring-svg" role="img" aria-label={`${Math.round(v)}%`}>
        <circle className="rv-ring-track" cx={size / 2} cy={size / 2} r={r} strokeWidth={stroke} fill="none" />
        <circle
          className={`rv-ring-fill ${tone}`} cx={size / 2} cy={size / 2} r={r}
          strokeWidth={stroke} fill="none" strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c * (1 - v / 100)}
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      </svg>
      <div className="rv-ring-center">
        <b className="rv-ring-num">{center ?? `${Math.round(v)}%`}</b>
        {sub && <span className="rv-ring-sub">{sub}</span>}
      </div>
    </div>
  );
}

/** 迷你柱状趋势（KPI 卡 / 路由档位分布共用）。 */
function Spark({ values, tone = 'accent', labels }: {
  values: number[]; tone?: Tone; labels?: string[];
}) {
  const W = 120;
  const H = 32;
  // 防御：任何 NaN/Infinity 进入 SVG 属性都会让整块图表（甚至页面）渲染异常
  const vals = values.map((x) => toNum(x));
  const max = Math.max(1, ...vals);
  const n = Math.max(1, vals.length);
  const bw = W / n;
  return (
    <svg className="rv-spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
      {vals.map((v, i) => {
        const h = v > 0 ? Math.max(2, (v / max) * (H - 3)) : 1.4;
        return (
          <rect
            key={i} className={`rv-spark-bar ${tone}`} rx={0.9}
            x={i * bw + 0.6} y={H - h} width={Math.max(1.2, bw - 1.4)} height={h}
          >
            <title>{labels?.[i] ? `${labels[i]}：${fmtInt(v)}` : fmtInt(v)}</title>
          </rect>
        );
      })}
    </svg>
  );
}

/** 30 天成本 / Token 柱状图（带网格与坐标轴）。 */
function CostChart({ days, values, mode }: {
  days: string[]; values: number[]; mode: 'cost' | 'tokens';
}) {
  const W = 680;
  const H = 200;
  const padL = 52;
  const padT = 12;
  const padB = 24;
  const innerW = W - padL - 10;
  const innerH = H - padT - padB;
  const vals = values.map((x) => Math.max(0, toNum(x)));
  const max = Math.max(1, ...vals);
  const bw = innerW / Math.max(1, days.length);
  const grid = [0, 0.25, 0.5, 0.75, 1];
  const tickText = (v: number) => (mode === 'cost' ? `¥${fmtMoneyAxis(v)}` : fmtTokens(v));
  return (
    <svg
      className="rv-chart" viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label={mode === 'cost' ? '近 30 天成本柱状图' : '近 30 天 Token 柱状图'}
    >
      {grid.map((f, i) => (
        <line
          key={i} className="rv-chart-grid" x1={padL} x2={W - 10}
          y1={padT + innerH * (1 - f)} y2={padT + innerH * (1 - f)}
        />
      ))}
      {vals.map((v, i) => {
        const h = v > 0 ? Math.max(2, (v / max) * innerH) : 0;
        return (
          <rect
            key={i} rx={1.4}
            className={`rv-chart-bar ${mode === 'tokens' ? 'alt' : ''}`}
            x={padL + i * bw + bw * 0.16} y={padT + innerH - h}
            width={Math.max(2, bw * 0.68)} height={h}
          >
            <title>{`${days[i]}：${mode === 'cost' ? `¥${fmtMoney(v)}` : `${fmtInt(v)} tk`}`}</title>
          </rect>
        );
      })}
      {days.map((d, i) => (i % 5 === 0 ? (
        <text key={d} className="rv-chart-axis" x={padL + i * bw + bw / 2} y={H - 7} textAnchor="middle">
          {shortDay(d)}
        </text>
      ) : null))}
      {[0, 0.5, 1].map((f, i) => (
        <text
          key={`t${i}`} className="rv-chart-axis" x={padL - 7}
          y={padT + innerH * (1 - f) + 3} textAnchor="end"
        >
          {tickText(max * f)}
        </text>
      ))}
    </svg>
  );
}

// ══════════════════════════════════════════════════════════════
// 通用小卡片
// ══════════════════════════════════════════════════════════════

function SectionCard({ icon, title, tags, actions, sub, children, className = '' }: {
  icon: ReactNode; title: ReactNode; tags?: ReactNode;
  actions?: ReactNode; sub?: ReactNode;
  children: ReactNode; className?: string;
}) {
  return (
    <section className={`rv-card ${className}`}>
      <header className="rv-card-head">
        <div className="rv-card-title">
          <span className="rv-card-ico">{icon}</span>
          <span>{title}</span>
          {tags}
        </div>
        {sub && <div className="rv-card-sub">{sub}</div>}
        {actions && <div className="rv-card-actions">{actions}</div>}
      </header>
      <div className="rv-card-body">{children}</div>
    </section>
  );
}

/**
 * 给 AsyncBoundary 用的视图态。
 *
 * useAsync 在"功能未解锁"时会先 resolve(null)，把 loaded 置 true；等 /status
 * 到达、功能解锁后重新取数若失败，AsyncBoundary 会误判成"刷新失败、下方是上一次
 * 的数据"——其实根本没有上一次数据（只有一个空卡片）。这里把"data 为 null"视为
 * 未加载，让失败一律走错误面板；有旧数据时的静默刷新仍保留原来的横向错误条。
 */
function toBoundaryState<T>(st: {
  data: T | undefined; loading: boolean; error: Error | null;
  loaded: boolean; reload: () => void;
}) {
  return {
    data: st.data ?? undefined,
    loading: st.loading,
    error: st.error,
    loaded: st.loaded && st.data != null,
    reload: st.reload,
  };
}

function Stat({ label, value, foot, tone }: {
  label: ReactNode; value: ReactNode; foot?: ReactNode; tone?: Tone;
}) {
  return (
    <div className={`rv-stat${tone ? ` tone-${tone}` : ''}`}>
      <div className="rv-stat-label">{label}</div>
      <div className="rv-stat-value">{value}</div>
      {foot && <div className="rv-stat-foot">{foot}</div>}
    </div>
  );
}

interface KpiStackSeg { label: string; value: number; tone?: Tone }

interface KpiDef {
  key: string; icon: string; label: string; value: ReactNode; unit?: string;
  foot: ReactNode; locked: boolean; lockText: string; tone?: Tone;
  spark?: number[]; sparkLabels?: string[]; meter?: number; stack?: KpiStackSeg[];
}

function Kpi({ k }: { k: KpiDef }) {
  const stackSegs = (k.stack || []).map((s) => ({ ...s, value: Math.max(0, toNum(s.value)) }));
  const stackTotal = stackSegs.reduce((a, s) => a + s.value, 0);
  return (
    <div className={`rv-kpi${k.locked ? ' locked' : ''}${k.tone ? ` tone-${k.tone}` : ''}`}>
      <div className="rv-kpi-top">
        <span className="rv-kpi-ico">{k.icon}</span>
        <span className="rv-kpi-label">{k.label}</span>
        {k.locked && <span className="rv-kpi-lock" title={k.lockText}>🔒</span>}
      </div>
      <div className="rv-kpi-value">
        {k.value}{k.unit && <small>{k.unit}</small>}
      </div>
      {k.spark && k.spark.length > 0 ? (
        <Spark values={k.spark} tone={k.tone || 'accent'} labels={k.sparkLabels} />
      ) : k.stack && k.stack.length > 0 ? (
        <div className="rv-stackbar">
          {k.stack.map((s) => (
            <i
              key={s.label}
              className={s.tone || 'accent'}
              style={{ width: `${stackTotal > 0 ? (Math.max(0, s.value) / stackTotal) * 100 : 0}%` }}
              title={`${s.label}：${fmtInt(s.value)} 次`}
            />
          ))}
        </div>
      ) : k.meter != null ? (
        <div className="rv-meter rv-kpi-meter">
          <i className="accent" style={{ width: `${clamp(k.meter)}%` }} />
        </div>
      ) : (
        <div className="rv-spark rv-spark-empty" />
      )}
      <div className="rv-kpi-foot">
        <span>{k.foot}</span>
        {k.locked && <span className="rv-kpi-locktext">{k.lockText}</span>}
      </div>
    </div>
  );
}

/** 未解锁能力的统一渐变卡：功能名 + 收益 + 升级 CTA。 */
function LockedCard({ icon, title, tier, benefits }: {
  icon: string; title: string; tier: string; benefits: string[];
}) {
  return (
    <section className="rv-locked">
      <div className="rv-locked-ico" aria-hidden="true">{icon}</div>
      <div className="rv-locked-main">
        <div className="rv-locked-title">
          {title}
          <span className="rv-lock-tag">🔒 {tier}功能</span>
        </div>
        <ul className="rv-locked-list">
          {benefits.map((b) => <li key={b}>{b}</li>)}
        </ul>
      </div>
      <button
        type="button" className="rv-cta"
        onClick={() => useUi.getState().openModal('license')}
      >
        了解升级 / 激活 →
      </button>
    </section>
  );
}

// ══════════════════════════════════════════════════════════════
// 语义缓存卡
// ══════════════════════════════════════════════════════════════

function CacheCard({ cache, savedCost, setCache, reload }: {
  cache: CacheStats; savedCost: number | null;
  setCache: (v: CacheStats) => void; reload: () => void;
}) {
  const { message, modal } = App.useApp();
  // 根因修复（v1.11.16）：旧版服务端不返回 threshold 时，原实现是
  // `useState<number>(cache.threshold)` → thr=undefined → `thr.toFixed(2)` 抛
  // TypeError，整页白屏。这里初始值/同步值都过 safeThreshold（非法回 0.93），
  // 且下方滑杆只在服务端确实上报了合法阈值时才渲染。
  const [thr, setThr] = useState<number>(() => safeThreshold(cache.threshold));
  const [busy, setBusy] = useState(false);

  useEffect(() => { setThr(safeThreshold(cache.threshold)); }, [cache.threshold]);

  const apply = async (patch: Record<string, unknown>, okMsg?: string) => {
    setBusy(true);
    try {
      const next = await apiPost<unknown>('/cache/config', patch);
      setCache(sanitizeCache(guardPayload(next)));
      if (okMsg) message.success(okMsg);
    } catch (e: any) {
      message.error(`保存失败：${e?.friendly || e?.message || e}`);
      // 阈值滑杆是"先动后存"：保存失败要把滑杆位置退回去，否则界面显示的
      // 是没生效的值，下次刷新又跳变，用户会以为设置丢过一次
      if ('threshold' in patch) setThr(safeThreshold(cache.threshold));
    } finally {
      setBusy(false);
    }
  };

  const clear = () => modal.confirm({
    title: '清空语义缓存？',
    content: '已缓存的历史问答会被删除，之后相似问题不再秒回、需要重新调用模型（会消耗 Token）。'
      + '命中次数与会话统计不会清零。',
    okText: '清空缓存', cancelText: '取消', okButtonProps: { danger: true },
    onOk: async () => {
      try {
        const r = await apiDelete('/cache');
        message.info(`已清空 ${fmtInt(asRecord(r).cleared)} 条缓存`);
        reload();
      } catch (e: any) {
        message.error(`清空失败：${e?.friendly || e?.message || e}`);
      }
    },
  });

  const hitRate = clamp(cache.hit_rate);
  const capPct = toNum(cache.capacity) > 0
    ? clamp((toNum(cache.entries) / toNum(cache.capacity)) * 100)
    : 0;
  const thrLabel = fixed(thr, 2);

  return (
    <SectionCard
      icon="⚡" title="语义缓存"
      tags={<>
        {cache.advanced
          ? <span className="rv-tag purple">企业版 · 高级</span>
          : <span className="rv-tag accent">专业版 · 基础</span>}
        <span className={`rv-tag ${cache.enabled ? 'ok' : 'muted'}`}>
          {cache.enabled ? '已启用' : '已停用'}
        </span>
      </>}
      actions={<>
        <span className="rv-action-label">启用</span>
        <Switch
          size="small" checkedChildren="开" unCheckedChildren="关"
          checked={cache.enabled} loading={busy}
          onChange={(v) => apply({ enabled: v }, v ? '语义缓存已开启' : '语义缓存已关闭')}
        />
        <Button size="small" danger onClick={clear}>清空缓存</Button>
      </>}
    >
      {cache._degraded && cache._degraded.length > 0 && (
        <div className="rv-banner warn">
          ⚠️ 服务端返回的数据不完整：{cache._degraded.join('；')}。已按默认值展示，不影响页面其它功能。
        </div>
      )}
      <div className="rv-cache-grid">
        <div className="rv-ring-wrap">
          <Ring pct={hitRate} center={pctText(hitRate)} sub="缓存命中率" tone={hitRate >= 60 ? 'green' : hitRate >= 30 ? 'accent' : 'yellow'} />
          <div className="rv-ring-foot">
            {fmtInt(cache.hits)} / {fmtInt(cache.lookups)} 次查询命中
          </div>
        </div>
        <div className="rv-cache-main">
          <div className="rv-stats">
            <Stat label="缓存条目" value={fmtInt(cache.entries)} foot={`容量 ${fmtInt(cache.capacity)}`} />
            <Stat label="命中次数" value={fmtInt(cache.hits)} foot={`共 ${fmtInt(cache.lookups)} 次查询`} />
            <Stat label="节省 Token" value={fmtTokens(cache.saved_tokens)} foot="相似问题直接秒回" tone="green" />
            <Stat
              label="估算节省金额"
              value={savedCost != null ? `¥${fmtMoney(savedCost)}` : '—'}
              foot={savedCost != null ? '按历史输出单价估算' : '企业版成本仪表盘口径'}
              tone="yellow"
            />
            <Stat label="TTL" value={cache.ttl_hours != null ? `${cache.ttl_hours} h` : '—'} foot={cache.advanced ? '企业版更长' : '基础版 24h'} />
            <Stat label="相似阈值" value={thrLabel} foot="越高越严格" />
          </div>
          <div className="rv-cap">
            <div className="rv-cap-head">
              <span>容量使用</span>
              <b>{fmtInt(cache.entries)} / {fmtInt(cache.capacity)}</b>
            </div>
            <div className="rv-meter">
              <i className={capPct >= 90 ? 'red' : capPct >= 70 ? 'yellow' : 'accent'} style={{ width: `${capPct}%` }} />
            </div>
          </div>
          {cache.threshold_valid === false ? (
            // 服务端没上报合法阈值：滑杆既不能反映真实配置、保存也可能被旧服务端
            // 忽略 —— 直接隐藏，只展示默认值，避免"拖了没反应"的误解。
            <div className="rv-slider-row rv-slider-row-static">
              <span className="rv-slider-label">相似阈值</span>
              <span className="rv-slider-val">{thrLabel}</span>
              <span className="rv-hint">
                服务端未上报该字段，暂不可调整（按默认 {fixed(DEFAULT_THRESHOLD, 2)} 展示）。
              </span>
            </div>
          ) : (
            <div className="rv-slider-row">
              <span className="rv-slider-label">相似阈值</span>
              <Slider
                className="rv-slider" min={0.5} max={0.999} step={0.01}
                value={thr} disabled={busy}
                onChange={(v) => setThr(safeThreshold(v))}
                onChangeComplete={(v) => apply({ threshold: v }, `相似阈值已设为 ${fixed(v, 2)}`)}
                tooltip={{ formatter: (v) => fixed(v, 2) }}
              />
              <span className="rv-slider-val">{thrLabel}</span>
            </div>
          )}
          <p className="rv-hint">
            对话模式下相似问题直接秒回历史答案（0 Token）。
            {cache.advanced
              ? '企业版：容量 2000 · TTL 7 天 · 节省额度计入成本仪表盘。'
              : `基础版：容量 ${cache.capacity} · TTL ${cache.ttl_hours ?? 24}h；升级企业版可扩容到 2000 条。`}
          </p>
        </div>
      </div>
    </SectionCard>
  );
}

// ══════════════════════════════════════════════════════════════
// 模型智能路由卡
// ══════════════════════════════════════════════════════════════

function RouterCard({ router, reload }: { router: RouterCfg; reload: () => void }) {
  const { message } = App.useApp();
  // props 已经过 sanitizeRouter；这里再复制一份，避免编辑态直接改到上游数据
  const [tiers, setTiers] = useState<RouterTier[]>(() => (
    router.tiers.length ? router.tiers.map((t) => ({ ...t })) : DEFAULT_TIERS.map((t) => ({ ...t }))
  ));
  const [providers, setProviders] = useState<Providers | null>(null);
  const [provErr, setProvErr] = useState('');
  const [saving, setSaving] = useState(false);

  // 保存/重载后以服务端配置为准；本地编辑期间父组件重渲染不会覆盖（router 引用未变）
  useEffect(() => {
    setTiers(router.tiers.length
      ? router.tiers.map((t) => ({ ...t }))
      : DEFAULT_TIERS.map((t) => ({ ...t })));
  }, [router]);

  useEffect(() => {
    apiGet<unknown>('/providers')
      .then((raw) => setProviders(sanitizeProviders(guardPayload(raw))))
      .catch((e) => setProvErr(e?.friendly || e?.message || String(e)));
  }, []);

  const maxTiers = router.max_tiers;
  const atLimit = maxTiers != null && tiers.length >= maxTiers;
  const allP = providers
    ? [...(providers.cloud || []), ...(providers.local || []), ...(providers.custom || [])]
    : [];
  // 供应商清单可能没拉回来 / 当前档位用的 provider 不在列表里：也要能显示出来
  const provOptions = useMemo(() => {
    const set = new Set(allP);
    for (const t of tiers) if (t.provider) set.add(t.provider);
    return [...set].map((p) => ({ value: p, label: providers?.labels?.[p] || p }));
  }, [allP, providers, tiers]);

  const patch = (i: number, p: Partial<RouterTier>) => setTiers((cur) =>
    cur.map((x, k) => (k === i ? { ...x, ...p } : x)));

  const save = async (enabled?: boolean) => {
    setSaving(true);
    try {
      const r = await apiPost<unknown>('/router', {
        tiers, ...(enabled !== undefined ? { enabled } : {}),
      });
      const err = str(asRecord(r).error);
      if (err) { message.error(err); return; }
      message.success(enabled !== undefined
        ? (enabled ? '路由已启用并保存' : '路由已停用')
        : '路由配置已保存');
      reload();
    } catch (e: any) {
      message.error(`保存失败：${e?.friendly || e?.message || e}`);
    } finally {
      setSaving(false);
    }
  };

  // 阶梯区间：按 max_score 升序，最后一段一直覆盖到 100（超过所有上限回落最强档）
  const sorted = useMemo(
    () => [...tiers].sort((a, b) => toNum(a.max_score) - toNum(b.max_score)),
    [tiers]);
  const bounds = useMemo(() => {
    let prev = 0;
    return sorted.map((t, i) => {
      const to = i === sorted.length - 1 ? 100 : clamp(t.max_score);
      // lo/hi 是"人读的命中区间"（score ≤ max_score，故下一档从上一档 +1 起）；
      // width 用边界值计算，保证阶梯铺满 0~100 且不重叠。
      const seg = {
        ...t, lo: i === 0 ? 0 : prev + 1, hi: to,
        width: Math.max(3, to - prev),
      };
      prev = to;
      return seg;
    });
  }, [sorted]);

  // 统计来自旧配置时可能是字符串/负数/NaN —— 一律先归一到有限非负数
  const stats: [string, number][] = Object.entries(asRecord(router.stats))
    .map(([name, v]) => [name, Math.max(0, toNum(v))] as [string, number]);
  const totalHits = stats.reduce((a, [, v]) => a + v, 0);
  const statMax = Math.max(1, ...stats.map(([, v]) => v));
  const enabled = !!router.enabled;

  return (
    <SectionCard
      icon="🧭" title="模型智能路由"
      tags={
        maxTiers == null
          ? <span className="rv-tag purple">企业版 · N 级</span>
          : <span className="rv-tag accent">专业版 · {maxTiers} 级</span>
      }
      sub={<span className="rv-card-subtext">按任务复杂度自动选模型，简单任务省钱、复杂任务用强模型</span>}
      actions={<>
        <span className={`rv-tag ${enabled ? 'ok' : 'muted'}`}>{enabled ? '已启用' : '未启用'}</span>
        <Switch
          size="small" checkedChildren="开" unCheckedChildren="关"
          checked={enabled} loading={saving}
          onChange={(v) => save(v)}
        />
      </>}
    >
      {!enabled && (
        <div className="rv-banner warn">
          ⚠️ 路由当前未启用：所有任务都使用默认模型。打开右上角开关即可按复杂度分流。
        </div>
      )}
      {router._degraded && router._degraded.length > 0 && (
        <div className="rv-banner warn">
          ⚠️ 路由配置不完整：{router._degraded.join('；')}。已按可用部分展示，保存会覆盖服务端配置。
        </div>
      )}
      <div className="rv-ladder">
        <div className="rv-ladder-bar">
          {bounds.map((b, i) => (
            <div
              key={i} className={`rv-ladder-seg t${i % TONE_CLASS.length}`}
              style={{ width: `${b.width}%` }}
              title={`${b.name || `档位${i + 1}`}：复杂度 ${b.lo}–${b.hi} · ${b.provider}/${b.model}`}
            >
              <span className="rv-ladder-name">{b.name || `档位${i + 1}`}</span>
              <span className="rv-ladder-range">{b.lo}–{b.hi}</span>
            </div>
          ))}
        </div>
        <div className="rv-ladder-scale">
          <span>0 · 简单</span><span>25</span><span>50</span><span>75</span><span>100 · 复杂</span>
        </div>
      </div>
      <p className="rv-hint">
        档位按 max_score 升序命中第一个满足 score ≤ max_score 的档；超过所有档位上限时回落到最强档。
        {router.tiers?.length ? '' : '（当前还没有保存过档位，下方为推荐默认值，保存后生效）'}
      </p>
      {provErr && (
        <div className="rv-banner error">
          ⚠️ 供应商清单加载失败：{provErr} —— 下方模型下拉可能是空的，可直接手填 provider / 模型名。
        </div>
      )}
      <div className="rv-tier-list">
        {tiers.map((t, i) => (
          <div className="rv-tier-row" key={i}>
            <span className={`rv-tier-dot t${i % TONE_CLASS.length}`} aria-hidden="true" />
            <Input
              size="small" className="rv-tier-name" value={t.name} placeholder={`档位${i + 1}`}
              onChange={(e) => patch(i, { name: e.target.value })}
            />
            <Select
              size="small" showSearch className="rv-tier-provider" value={t.provider}
              options={provOptions}
              onChange={(v: any) => patch(i, { provider: v })}
            />
            <Input
              size="small" className="rv-tier-model" value={t.model}
              placeholder="模型名，如 deepseek-chat"
              onChange={(e) => patch(i, { model: e.target.value })}
            />
            <div className="rv-tier-score">
              <span className="rv-hint">复杂度 ≤</span>
              <InputNumber
                size="small" min={1} max={100} value={t.max_score}
                onChange={(v) => patch(i, { max_score: clampNum(v, 1, 100, 100) })}
              />
            </div>
            <Button
              size="small" type="text" danger disabled={tiers.length <= 1}
              title="删除该档位" onClick={() => setTiers(tiers.filter((_, k) => k !== i))}
            >✕</Button>
          </div>
        ))}
      </div>
      <div className="rv-actions-row">
        <Button
          size="small" disabled={atLimit}
          onClick={() => setTiers([...tiers, {
            name: `档位${tiers.length + 1}`, provider: allP[0] || tiers[0]?.provider || 'openai',
            model: '', max_score: 100,
          }])}
        >➕ 加一档</Button>
        {atLimit && (
          <span className="rv-hint">专业版最多 {maxTiers} 级；企业版不限级数。</span>
        )}
        <span className="rv-actions-spacer" />
        <Button size="small" type="primary" loading={saving} onClick={() => save()}>
          保存档位
        </Button>
      </div>
      {totalHits > 0 ? (
        <div className="rv-router-stats">
          <div className="rv-router-stats-head">
            <b>路由统计</b>
            <span className="rv-hint">累计命中 {fmtInt(totalHits)} 次</span>
          </div>
          {stats.map(([name, count]) => (
            <div className="rv-tier-stat" key={name}>
              <span className="rv-tier-stat-name" title={name}>{name}</span>
              <div className="rv-tier-stat-bar">
                <i style={{ width: `${statMax > 0 ? clamp((count / statMax) * 100) : 0}%` }} />
              </div>
              <b>{fmtInt(count)}</b>
            </div>
          ))}
        </div>
      ) : (
        <p className="rv-hint">还没有路由记录：启用并保存档位后，下一次任务会按复杂度自动分流。</p>
      )}
    </SectionCard>
  );
}

// ══════════════════════════════════════════════════════════════
// 路由预演卡
// ══════════════════════════════════════════════════════════════

function PreviewCard({ router }: { router: RouterCfg }) {
  const { message } = App.useApp();
  const [task, setTask] = useState('');
  const [res, setRes] = useState<PreviewResp | null>(null);
  const [busy, setBusy] = useState(false);

  const tiersSorted = useMemo(
    () => [...(router.tiers || [])].sort((a, b) => a.max_score - b.max_score),
    [router.tiers]);

  const run = async () => {
    const text = task.trim();
    if (!text) { message.info('先输入一段任务文本再预演'); return; }
    setBusy(true);
    try {
      const raw = await apiPost<unknown>('/router/preview', { task: text });
      setRes(sanitizePreview(guardPayload(raw)));
    } catch (e: any) {
      message.error(`预演失败：${e?.friendly || e?.message || e}`);
    } finally {
      setBusy(false);
    }
  };

  // 本地预测命中档位：未启用时也告诉用户"如果启用会走哪一档"，而不是留一片空白
  const predicted = useMemo(() => {
    if (!res || !tiersSorted.length) return null;
    const score = clamp(res.score);
    return tiersSorted.find((t) => score <= toNum(t.max_score)) || tiersSorted[tiersSorted.length - 1];
  }, [res, tiersSorted]);

  const score = clamp(res?.score);

  return (
    <SectionCard
      icon="🧪" title="路由预演"
      sub={<span className="rv-card-subtext">只做本地复杂度打分，不调用模型、不消耗 Token</span>}
      tags={<span className="rv-tag muted">0 Token</span>}
    >
      <div className="rv-preview-input">
        <Input
          value={task} placeholder="输入任务文本，看看会被路由到哪一档…"
          onChange={(e) => setTask(e.target.value)}
          onPressEnter={run}
        />
        <Button type="primary" loading={busy} onClick={run}>预演</Button>
      </div>
      {!res ? (
        <div className="rv-preview-empty">
          <span className="rv-hint">试试这些例子：</span>
          {PREVIEW_SAMPLES.map((s) => (
            <button key={s} type="button" className="rv-chip" onClick={() => setTask(s)}>{s}</button>
          ))}
        </div>
      ) : (
        <div className="rv-preview-result">
          <div className="rv-ring-wrap">
            <Ring pct={score} center={score} sub="复杂度得分" tone={score >= 66 ? 'purple' : score >= 36 ? 'accent' : 'green'} />
            <div className="rv-ring-foot">满分 100</div>
          </div>
          <div className="rv-preview-main">
            {!router.enabled && (
              <div className="rv-banner warn">
                ⚠️ 路由未启用：预演只展示复杂度与「如果启用会命中的档位」，不会真的切换模型。
              </div>
            )}
            <div className="rv-preview-target">
              <span className="rv-preview-label">{router.enabled ? '命中档位' : '预测档位（未启用）'}</span>
              {predicted ? (
                <>
                  <span className="rv-tag accent">{predicted.name || '未命名档位'}</span>
                  <code className="rv-code">{predicted.provider}/{predicted.model}</code>
                </>
              ) : (
                <span className="rv-hint">没有可用档位：请先在「模型智能路由」里添加并保存档位。</span>
              )}
            </div>
            {tiersSorted.length > 0 && (
              <div className="rv-preview-tiers">
                {tiersSorted.map((t) => (
                  <span
                    key={`${t.name}-${t.max_score}`}
                    className={`rv-preview-tier${predicted === t ? ' on' : ''}`}
                    title={`复杂度 ≤ ${t.max_score} → ${t.provider}/${t.model}`}
                  >
                    {t.name || '档位'} <i>≤{t.max_score}</i>
                  </span>
                ))}
              </div>
            )}
            {router.enabled && !res.selected && (
              <p className="rv-hint">
                已启用但本次没有命中原因（档位为空或未保存）：任务仍走默认模型。
              </p>
            )}
            {router.enabled && res.selected && res.selected.tier !== predicted?.name && (
              <p className="rv-hint">
                服务端实际命中：{res.selected.tier}（{res.selected.provider}/{res.selected.model}），
                与本地预测不同，可能档位刚被修改。
              </p>
            )}
          </div>
        </div>
      )}
    </SectionCard>
  );
}

// ══════════════════════════════════════════════════════════════
// 成本仪表盘（企业版）
// ══════════════════════════════════════════════════════════════

interface ModelRow extends ModelCost { model: string }

function CostsCard({ costs, reload }: { costs: CostsData; reload: () => void }) {
  const [mode, setMode] = useState<'cost' | 'tokens'>('cost');
  const rangeDays = clampNum(costs.range_days, 1, 365, 30);
  const days = useMemo(() => lastDays(rangeDays), [rangeDays]);
  const costValues = useMemo(
    () => days.map((d) => Math.max(0, toNum(costs.by_day?.[d]?.cost))), [days, costs]);
  const tokenValues = useMemo(
    () => days.map((d) => Math.max(0, toNum(costs.by_day?.[d]?.tokens))), [days, costs]);
  const values = mode === 'cost' ? costValues : tokenValues;
  const cost30 = costValues.reduce((a, b) => a + b, 0);
  const token30 = tokenValues.reduce((a, b) => a + b, 0);
  const activeDays = costValues.filter((v) => v > 0).length;
  const avgCost = rangeDays > 0 ? cost30 / rangeDays : 0;
  const rows: ModelRow[] = Object.entries(asRecord(costs.by_model))
    .map(([model, v]) => {
      const o = asRecord(v);
      return {
        model,
        tasks: Math.max(0, toInt(o.tasks)),
        prompt: Math.max(0, toInt(o.prompt)),
        completion: Math.max(0, toInt(o.completion)),
        cost: Math.max(0, toNum(o.cost)),
        share: clampNum(o.share, 0, 1, 0),
      };
    });
  const totalTasks = toNum(costs.total_tasks, rows.reduce((a, r) => a + r.tasks, 0));
  const savedCost = costs.cache?.saved_cost;
  const savedTokens = costs.cache?.saved_tokens;
  const topTasks = costs.top_model ? toNum(costs.by_model?.[costs.top_model]?.tasks) : 0;
  const unknownCount = rows.filter((r) => r.cost === 0 && !/ollama|llama/i.test(r.model)).length;

  const columns: TableColumnsType<ModelRow> = [
    {
      title: '模型', dataIndex: 'model',
      render: (m: string) => (
        <span className="rv-model" title={m}>
          {m}{costs.top_model === m && <span className="rv-top-tag">主力</span>}
        </span>
      ),
    },
    {
      title: '任务数', dataIndex: 'tasks', width: 84, align: 'right',
      render: (v: number) => fmtInt(v),
    },
    {
      title: '输入 tk', dataIndex: 'prompt', width: 104, align: 'right',
      render: (v: number) => fmtInt(v),
    },
    {
      title: '输出 tk', dataIndex: 'completion', width: 104, align: 'right',
      render: (v: number) => fmtInt(v),
    },
    {
      title: '成本 ¥', dataIndex: 'cost', width: 100, align: 'right',
      render: (v: number) => (toNum(v) ? `¥${fmtMoney(v)}` : '—'),
    },
    {
      title: '成本占比', dataIndex: 'share', width: 168,
      render: (s: number, r: ModelRow) => {
        const pct = clamp(toNum(s) * 100);
        return (
          <div className="rv-share" title={`${r.model}：${fixed(pct, 1)}%`}>
            <div className="rv-share-track"><i style={{ width: `${pct}%` }} /></div>
            <span className="rv-share-num">{fixed(pct, 1)}%</span>
          </div>
        );
      },
    },
  ];

  return (
    <SectionCard
      icon="💰" title="成本仪表盘"
      tags={<span className="rv-tag purple">企业版</span>}
      sub={<span className="rv-card-subtext">按模型与日期聚合任务 Token，乘以内置单价表估算成本</span>}
      actions={<>
        <div className="rv-toggle" role="group" aria-label="切换图表口径">
          <button type="button" className={mode === 'cost' ? 'on' : ''} onClick={() => setMode('cost')}>金额 ¥</button>
          <button type="button" className={mode === 'tokens' ? 'on' : ''} onClick={() => setMode('tokens')}>Token</button>
        </div>
        <Button size="small" onClick={reload}>⟳ 刷新</Button>
      </>}
    >
      {costs._degraded && costs._degraded.length > 0 && (
        <div className="rv-banner warn">
          ⚠️ 成本数据不完整：{costs._degraded.join('；')}。已按可用数据展示。
        </div>
      )}
      {totalTasks === 0 && rows.length === 0 ? (
        <EmptyState
          icon="💰" title="还没有可统计的任务"
          hint="完成的任务会按模型和日期聚合到这里；企业版还会叠加语义缓存的节省额度。"
        />
      ) : (
        <>
          <div className="rv-cost-summary">
            <Stat label="累计估算成本" value={`¥${fmtMoney(costs.total_cost)}`} tone="accent"
              foot={`${fmtInt(totalTasks)} 个任务 · ${costs.currency || 'CNY'}`} />
            <Stat label="总 Token" value={fmtTokens(costs.total_tokens)} foot={`${fmtInt(costs.total_tokens)} tk`} />
            <Stat label="近 30 天成本" value={`¥${fmtMoney(cost30)}`} foot={`活跃 ${activeDays} 天 · 日均 ¥${fmtMoney(avgCost)}`} />
            <Stat label="缓存节省" value={savedCost != null ? `¥${fmtMoney(savedCost)}` : '—'}
              foot={savedTokens != null ? `${fmtTokens(savedTokens)} tk` : '语义缓存未启用'} tone="green" />
            <Stat label="主力模型" value={costs.top_model || '—'}
              foot={costs.top_model ? `${fmtInt(topTasks)} 个任务` : '暂无数据'} />
          </div>
          <div className="rv-chart-head">
            <b>近 {rangeDays} 天{mode === 'cost' ? '成本' : ' Token 用量'}</b>
            <span className="rv-hint">
              区间合计 {mode === 'cost' ? `¥${fmtMoney(cost30)}` : `${fmtInt(token30)} tk`}
            </span>
          </div>
          <CostChart days={days} values={values} mode={mode} />
          {unknownCount > 0 && (
            <p className="rv-hint">
              ⚠️ 有 {unknownCount} 个模型不在内置单价表中，其成本按 0 计（Token 用量仍正常统计）。
            </p>
          )}
          <div className="rv-table-wrap">
            <Table<ModelRow>
              className="rv-cost-table" size="small" rowKey="model"
              columns={columns} dataSource={rows} pagination={false}
              scroll={{ x: 720 }}
            />
          </div>
          {costs.cache && (
            <div className="rv-cost-cache">
              <div className="rv-cost-cache-main">
                <b>⚡ 语义缓存节省</b>
                <span className="rv-hint">
                  {costs.cache.saved_cost_note || '按缓存命中省下的回答 token 估算'}
                </span>
              </div>
              <div className="rv-cost-cache-nums">
                <span>节省 Token <b>{fmtTokens(costs.cache.saved_tokens)}</b></span>
                <span>估算节省 <b className="rv-green">¥{fmtMoney(costs.cache.saved_cost)}</b></span>
              </div>
            </div>
          )}
        </>
      )}
      <p className="rv-note">
        {costs.note}
        {' · '}币种 {costs.currency || 'CNY'}
        {' · '}统计区间近 {costs.range_days || 30} 天
        {' · '}生成于 {fmtTime(costs.generated_at)}
      </p>
    </SectionCard>
  );
}

// ══════════════════════════════════════════════════════════════
// 页面
// ══════════════════════════════════════════════════════════════

/**
 * 页面级容错（v1.11.16）。
 *
 * 用户报告的崩溃是"打开页面就白屏"：React 18 里任意组件在渲染中抛异常会卸载
 * 整棵树。这里两层兜底：
 *   1. 本组件外层的 ErrorBoundary 接住 RouterViewBody（Hook/派生计算/未预料的渲染错误）；
 *   2. 页面内部每张卡片再各自包一层 ErrorBoundary —— 单卡异常只降级该卡，
 *      其余卡片、刷新按钮与左侧导航都还能用。
 * 正常渲染时 ErrorBoundary 不产生额外 DOM，不影响原有栅格与间距。
 */
export default function RouterView() {
  return (
    <div className="rv-page">
      <ErrorBoundary name="路由与成本" resetKey="router-view">
        <RouterViewBody />
      </ErrorBoundary>
    </div>
  );
}

function RouterViewBody() {
  const featureOn = useApp((s) => s.featureOn);
  const edition = str(useApp((s) => s.edition), 'community');
  const version = str(useApp((s) => s.version));

  const hasCache = featureOn('semantic_cache');
  const hasRouter = featureOn('model_router');
  const hasCosts = featureOn('cost_dashboard');

  // 三个接口的响应统一"先过门控/错误检查，再 sanitize 成字段齐全、数值有限的对象"。
  // 任何缺字段/类型错误的旧版响应都不会把 undefined/NaN 带进渲染层。
  const cacheSt = useAsync<CacheStats | null>(
    () => (hasCache
      ? apiGet<unknown>('/cache').then(guardPayload).then(sanitizeCache)
      : Promise.resolve(null)), [hasCache]);
  const routerSt = useAsync<RouterCfg | null>(
    () => (hasRouter
      ? apiGet<unknown>('/router').then(guardPayload).then(sanitizeRouter)
      : Promise.resolve(null)), [hasRouter]);
  const costsSt = useAsync<CostsData | null>(
    () => (hasCosts
      ? apiGet<unknown>('/costs').then(guardPayload).then(sanitizeCosts)
      : Promise.resolve(null)), [hasCosts]);

  const cache = cacheSt.data ?? null;
  const router = routerSt.data ?? null;
  const costs = costsSt.data ?? null;

  const refreshAll = () => {
    if (hasCache) cacheSt.reload();
    if (hasRouter) routerSt.reload();
    if (hasCosts) costsSt.reload();
  };
  const refreshing = (hasCache && cacheSt.loading) || (hasRouter && routerSt.loading)
    || (hasCosts && costsSt.loading);

  // ── KPI（只用 /cache /router /costs 的数据；未解锁的显示锁，不伪装成 0） ──
  const days30 = useMemo(() => lastDays(30), []);
  const costSeries = useMemo(
    () => days30.map((d) => Math.max(0, toNum(costs?.by_day?.[d]?.cost))), [days30, costs]);
  const tokenSeries = useMemo(
    () => days30.map((d) => Math.max(0, toNum(costs?.by_day?.[d]?.tokens))), [days30, costs]);
  const cost30 = costSeries.reduce((a, b) => a + b, 0);
  const token30 = tokenSeries.reduce((a, b) => a + b, 0);
  const avgCost = cost30 / 30;
  const routerStats = useMemo(
    () => Object.entries(asRecord(router?.stats))
      .map(([name, v]) => [name, Math.max(0, toNum(v))] as [string, number]),
    [router]);
  const routerHits = routerStats.reduce((a, [, v]) => a + v, 0);
  const unlocked = [hasCache, hasRouter, hasCosts].filter(Boolean).length;
  const editionCls = edition === 'pro' ? 'pro' : edition === 'enterprise' ? 'enterprise' : 'community';
  const savedCacheCostRaw = costs?.cache?.saved_cost;
  const savedCacheCost = Number.isFinite(Number(savedCacheCostRaw))
    ? Math.max(0, toNum(savedCacheCostRaw))
    : null;

  const kpis: KpiDef[] = [
    {
      key: 'cost', icon: '💰', label: '累计估算成本', tone: 'accent',
      value: hasCosts && costs ? `¥${fmtMoney(costs.total_cost)}` : '—',
      spark: hasCosts && costs ? costSeries : undefined,
      foot: costs ? `${fmtInt(costs.total_tasks)} 个任务 · ${costs.currency || 'CNY'}` : '近 30 天成本趋势',
      locked: !hasCosts, lockText: '企业版解锁',
    },
    {
      key: 'tokens', icon: '🔤', label: '总 Token',
      value: hasCosts && costs ? fmtTokens(costs.total_tokens) : '—',
      spark: hasCosts && costs ? tokenSeries : undefined, tone: 'purple',
      foot: hasCosts && costs ? `近 30 天 ${fmtTokens(token30)} tk` : '包括输入与输出 token',
      locked: !hasCosts, lockText: '企业版解锁',
    },
    {
      key: 'cache', icon: '⚡', label: '缓存命中率', tone: 'green',
      value: cache ? pctText(cache.hit_rate) : '—',
      meter: cache ? clamp(cache.hit_rate) : undefined,
      foot: cache
        ? `${fmtInt(cache.hits)} / ${fmtInt(cache.lookups)} 次查询 · 省 ${fmtTokens(cache.saved_tokens)} tk`
        : '相似问题秒回、省 Token',
      locked: !hasCache, lockText: '专业版解锁',
    },
    {
      key: 'router', icon: '🧭', label: '路由命中次数',
      value: hasRouter && router ? fmtInt(routerHits) : '—',
      // 只有总数没有按日历史：用"档位分布"堆叠条代替趋势，避免画一条假的 30 天曲线
      stack: hasRouter && routerStats.length
        ? routerStats.map(([name, v], i) => ({
          label: name, value: v, tone: TONE_CLASS[i % TONE_CLASS.length],
        }))
        : undefined,
      foot: hasRouter && router
        ? (router.enabled ? `已启用 · ${router.tiers.length || 0} 个档位` : '路由未启用')
        : '按复杂度自动分流模型',
      locked: !hasRouter, lockText: '专业版解锁',
    },
    {
      key: 'avg', icon: '📈', label: '近 30 天日均成本', tone: 'yellow',
      value: hasCosts && costs ? `¥${fmtMoney(avgCost)}` : '—',
      spark: hasCosts && costs ? costSeries : undefined,
      foot: hasCosts && costs ? `区间合计 ¥${fmtMoney(cost30)}` : '按自然日平均',
      locked: !hasCosts, lockText: '企业版解锁',
    },
  ];

  return (
    <>
      {/* ── Hero ── */}
      <header className="rv-hero">
        <div className="rv-hero-ico" aria-hidden="true">🧭</div>
        <div className="rv-hero-main">
          <h2 className="rv-hero-title">
            智能路由与成本
            <span className="rv-hero-grad">AutoMind</span>
          </h2>
          <p className="rv-hero-sub">
            语义缓存秒回相似问题 · 按任务复杂度分级选模型 · 成本与节省一目了然
          </p>
          <div className="rv-hero-tags">
            <span className={`rv-pill edition-${editionCls}`}>
              {EDITION_LABELS[edition] || edition || '社区版'}
            </span>
            {version && <span className="rv-pill">v{version}</span>}
            <span className="rv-pill muted">{unlocked} / 3 项能力已解锁</span>
            <span className="rv-pill muted">数据来源：/cache · /router · /costs</span>
          </div>
        </div>
        <div className="rv-hero-actions">
          <Button onClick={refreshAll} loading={refreshing}>⟳ 刷新数据</Button>
        </div>
      </header>

      {/* ── KPI 行 ── */}
      <ErrorBoundary name="KPI 概览">
        <div className="rv-kpis">
          {kpis.map((k) => <Kpi key={k.key} k={k} />)}
        </div>
      </ErrorBoundary>

      {/* ── 语义缓存 ── */}
      <ErrorBoundary name="语义缓存">
        {hasCache ? (
          <AsyncBoundary state={toBoundaryState(cacheSt)} what="语义缓存">
            {(d) => (d
              ? <CacheCard cache={d} savedCost={savedCacheCost}
                  setCache={cacheSt.setData} reload={cacheSt.reload} />
              : <div className="rv-card"><Skeleton active paragraph={{ rows: 4 }} /></div>)}
          </AsyncBoundary>
        ) : (
          <LockedCard
            icon="⚡" title="语义缓存" tier="专业版"
            benefits={[
              '相似问题直接秒回历史答案，0 Token',
              '命中率 / 节省 Token 实时统计',
              '相似阈值可调，企业版容量扩到 2000 条、TTL 7 天',
            ]}
          />
        )}
      </ErrorBoundary>

      {/* ── 模型智能路由 ── */}
      <ErrorBoundary name="模型智能路由">
        {hasRouter ? (
          <AsyncBoundary state={toBoundaryState(routerSt)} what="路由配置">
            {(d) => (d
              ? <RouterCard router={d} reload={routerSt.reload} />
              : <div className="rv-card"><Skeleton active paragraph={{ rows: 4 }} /></div>)}
          </AsyncBoundary>
        ) : (
          <LockedCard
            icon="🧭" title="模型智能路由" tier="专业版"
            benefits={[
              '按任务复杂度 0~100 启发式打分，自动选便宜/强力模型',
              '专业版 2 级路由，企业版不限级数、每档自定义复杂度区间',
              '路由命中统计与预演，不消耗 Token',
            ]}
          />
        )}
      </ErrorBoundary>

      {/* ── 路由预演 ── */}
      {hasRouter && router ? (
        <ErrorBoundary name="路由预演">
          <PreviewCard router={router} />
        </ErrorBoundary>
      ) : null}

      {/* ── 成本仪表盘 ── */}
      <ErrorBoundary name="成本仪表盘">
        {hasCosts ? (
          <AsyncBoundary state={toBoundaryState(costsSt)} what="成本仪表盘">
            {(d) => (d
              ? <CostsCard costs={d} reload={costsSt.reload} />
              : <div className="rv-card"><Skeleton active paragraph={{ rows: 4 }} /></div>)}
          </AsyncBoundary>
        ) : (
          <LockedCard
            icon="💰" title="成本仪表盘" tier="企业版"
            benefits={[
              '近 30 天成本 / Token 趋势柱状图',
              '按模型统计任务数、输入输出 Token 与成本占比',
              '语义缓存节省金额估算 + 单价口径说明',
            ]}
          />
        )}
      </ErrorBoundary>
    </>
  );
}
