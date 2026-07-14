// Token 成本估算（¥/百万 token，公开定价粗估；可自定义覆盖，存 localStorage）
const MODEL_PRICES: [string, number, number][] = [
  ['deepseek-reasoner', 4, 16], ['deepseek', 2, 8],
  ['gpt-4o-mini', 1.1, 4.4], ['gpt-4o', 18, 72], ['gpt-4.1-mini', 3, 12], ['gpt-4.1', 14, 57],
  ['o4-mini', 8, 32], ['o3', 14, 57],
  ['claude-3-5-haiku', 6, 29], ['claude-haiku', 6, 29], ['claude', 22, 108],
  ['kimi', 12, 12], ['moonshot', 12, 12],
  ['glm-4-flash', 0.1, 0.1], ['glm', 5, 5],
  ['qwen-turbo', 0.3, 0.6], ['qwen-plus', 0.8, 2], ['qwen', 2, 6],
  ['doubao', 0.8, 2],
  ['gemini-2.0-flash', 0.8, 3], ['gemini-1.5-flash', 0.6, 2.4], ['gemini', 9, 36],
  ['grok', 22, 108], ['ollama', 0, 0], ['llama', 0, 0],
];

export function modelPrice(model: string): [number | null, number | null, boolean] {
  try {
    const ov = JSON.parse(localStorage.getItem('automind_price') || 'null');
    if (ov && ov.prompt >= 0 && ov.completion >= 0) return [ov.prompt, ov.completion, true];
  } catch { /* ignore */ }
  const m = (model || '').toLowerCase();
  for (const [key, p, c] of MODEL_PRICES) if (m.includes(key)) return [p, c, false];
  return [null, null, false];
}

export function estCost(model: string, promptTk?: number, completionTk?: number): number | null {
  const [p, c] = modelPrice(model);
  if (p == null || c == null) return null;
  return ((promptTk || 0) / 1e6) * p + ((completionTk || 0) / 1e6) * c;
}

export function fmtCost(v: number | null): string | null {
  if (v == null) return null;
  return '≈¥' + (v < 0.01 ? v.toFixed(4) : v.toFixed(2));
}

export function setCustomPrice(prompt: number, completion: number) {
  localStorage.setItem('automind_price', JSON.stringify({ prompt, completion }));
}
export function clearCustomPrice() {
  localStorage.removeItem('automind_price');
}
