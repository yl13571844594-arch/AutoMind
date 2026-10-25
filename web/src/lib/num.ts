// 数值防御工具（v1.11.16）
//
// 背景：`路由与成本` 页的三个接口（/api/cache、/api/router、/api/costs）来自
// 不同版本的商业扩展包 —— 旧扩展包/字段裁剪/响应不完整时，字段可能是
// `undefined`、`null`、字符串数字（"12.5"）甚至彻底缺失。前端此前直接
// `undefined.toFixed()` 或让 NaN 流进 SVG 属性，会让**整页白屏**。
//
// 这里把「取数」和「格式化」两件事收口：
//   · toNum / toInt / toBool / asRecord / asArray / str —— 任何值都能安全取值；
//   · fixed / pctText —— 只有 Number.isFinite 才调用 toFixed，否则给 fallback。
// 调用方不再需要自己写 `x ?? 0` 与 `Number.isFinite` 组合（写漏一处就是白屏）。

/** 安全转数字：非有限值（undefined/null/""/NaN/对象/Infinity）一律用 fallback。 */
export function toNum(v: unknown, fallback = 0): number {
  if (typeof v === 'number') return Number.isFinite(v) ? v : fallback;
  if (typeof v === 'string') {
    const s = v.trim();
    if (!s) return fallback;
    const n = Number(s);
    return Number.isFinite(n) ? n : fallback;
  }
  if (typeof v === 'boolean') return v ? 1 : 0;
  return fallback;
}

/** 安全转整数（截断）。 */
export function toInt(v: unknown, fallback = 0): number {
  return Math.trunc(toNum(v, fallback));
}

/** 安全转布尔：兼容 true/false、0/1、"true"/"yes"/"on"/"enabled" 等。 */
export function toBool(v: unknown, fallback = false): boolean {
  if (typeof v === 'boolean') return v;
  if (typeof v === 'number') return v !== 0;
  if (typeof v === 'string') {
    const s = v.trim().toLowerCase();
    if (['1', 'true', 'yes', 'on', 'enabled', 'enable'].includes(s)) return true;
    if (['0', 'false', 'no', 'off', 'disabled', 'disable', ''].includes(s)) return false;
  }
  return fallback;
}

/** 把值限制在 [lo, hi]；非有限值用 fallback（默认 lo）。 */
export function clampNum(v: unknown, lo: number, hi: number, fallback?: number): number {
  const n = toNum(v, fallback === undefined ? lo : fallback);
  return Math.min(hi, Math.max(lo, n));
}

/** 仅当值是有限数字时才 toFixed；否则返回 fallback（默认 "—"）。 */
export function fixed(v: unknown, digits = 2, fallback = '—'): string {
  const n = typeof v === 'number' ? v : (typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN);
  return Number.isFinite(n) ? n.toFixed(Math.max(0, Math.min(20, digits))) : fallback;
}

/** 百分比文本：有限值 → "12.3%"，否则 fallback。 */
export function pctText(v: unknown, digits = 1, fallback = '—'): string {
  const n = typeof v === 'number' ? v : (typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN);
  return Number.isFinite(n) ? `${n.toFixed(Math.max(0, Math.min(20, digits)))}%` : fallback;
}

/** 安全当对象用：null/数组/标量 → {}（避免对非对象做 Object.entries 抛错）。 */
export function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

/** 安全当数组用：非数组 → []。 */
export function asArray(v: unknown): unknown[] {
  return Array.isArray(v) ? v : [];
}

/** 安全转字符串：string 原样，number/boolean 转文本，其余用 fallback。 */
export function str(v: unknown, fallback = ''): string {
  if (typeof v === 'string') return v;
  if (typeof v === 'number' && Number.isFinite(v)) return String(v);
  if (typeof v === 'boolean') return String(v);
  return fallback;
}
