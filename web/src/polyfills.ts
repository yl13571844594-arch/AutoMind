// 旧内核 API 兜底（v1.11.3）。
//
// 构建目标 es2017 只会把**语法**降级（可选链、空值合并…），不会补**API**。
// 产物里有几处未加判断的新 API：Object.fromEntries（Chrome 73）、Array#flat /
// flatMap（Chrome 69）、Promise.allSettled（Chrome 76）。旧内核走到这些地方会
// 直接抛 TypeError —— 表现为"点了没反应"。这里只补用得到的几个，按需定义、
// 不覆盖原生实现，体积可忽略。必须在其它任何模块之前导入（见 main.tsx）。

/* eslint-disable no-extend-native */
const define = (obj: any, name: string, value: unknown) => {
  if (obj && typeof obj[name] === 'undefined') {
    Object.defineProperty(obj, name, { value, configurable: true, writable: true });
  }
};

define(Object, 'fromEntries', function fromEntries(iter: Iterable<[PropertyKey, unknown]>) {
  const out: Record<PropertyKey, unknown> = {};
  for (const [k, v] of iter as any) out[k as any] = v;
  return out;
});

define(Array.prototype, 'flat', function flat(this: unknown[], depth = 1): unknown[] {
  const walk = (arr: unknown[], d: number): unknown[] => arr.reduce<unknown[]>(
    (acc, x) => acc.concat(Array.isArray(x) && d > 0 ? walk(x, d - 1) : [x]), []);
  return walk(this, Math.floor(Number(depth)) || 0);
});

define(Array.prototype, 'flatMap', function flatMap(
  this: unknown[], fn: (x: unknown, i: number, a: unknown[]) => unknown, thisArg?: unknown,
) {
  return (this.map(fn, thisArg) as any).flat(1);
});

define(Promise, 'allSettled', function allSettled(items: Iterable<unknown>) {
  return Promise.all(Array.from(items as any, (p) => Promise.resolve(p).then(
    (value) => ({ status: 'fulfilled', value }),
    (reason) => ({ status: 'rejected', reason }),
  )));
});

define(typeof window !== 'undefined' ? window : {}, 'queueMicrotask', (cb: () => void) => {
  Promise.resolve().then(cb).catch((e) => setTimeout(() => { throw e; }));
});

define(typeof window !== 'undefined' ? window : {}, 'globalThis', typeof window !== 'undefined' ? window : undefined);

export {};
