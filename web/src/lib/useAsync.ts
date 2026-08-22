// 数据加载的三态钩子：加载中 / 出错 / 有数据。
//
// 此前各视图统一是 `apiGet(...).then(setX).catch(() => {})`：
//   · 没有 loading 态 —— 首屏渲染时 state 还是初始的空数组，于是
//     **加载中被画成"暂无数据"**。这是最误导的一处：用户以为知识库是空的、
//     历史是空的，其实只是还没取回来（慢一点的机器上能空好几秒）。
//   · 失败被 `catch(() => {})` 吞掉 —— 服务挂了、令牌过期、接口 500，
//     界面同样显示"暂无数据"，用户完全无从判断到底出了什么事。
//
// 三态一旦分开，界面就能分别说："正在加载""加载失败（+重试）""确实没有数据"。
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../api/client';

export interface AsyncState<T> {
  data: T | undefined;
  loading: boolean;
  error: ApiError | Error | null;
  /** 是否已经至少成功加载过一次（用于"静默刷新"不闪 loading） */
  loaded: boolean;
  reload: () => void;
  /** 就地替换数据 —— 写接口已经把最新状态回给我们时，不必再多跑一次 GET。 */
  setData: (d: T) => void;
}

/**
 * 执行一次异步取数并跟踪三态。
 *
 * @param fn   取数函数；deps 变化时重跑。
 * @param deps 依赖数组，语义同 useEffect。
 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [tick, setTick] = useState(0);
  // 组件卸载后不要 setState —— 慢请求返回时组件早已不在，会报 warning
  const alive = useRef(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fnRef.current()
      .then((d) => {
        if (cancelled || !alive.current) return;
        setData(d);
        setLoaded(true);
      })
      .catch((e) => {
        if (cancelled || !alive.current) return;
        // 出错时**保留上一次的数据**：刷新失败不该把已经显示的内容清空，
        // 那会让一次网络抖动看起来像"数据没了"
        setError(e instanceof Error ? e : new Error(String(e)));
      })
      .finally(() => {
        if (!cancelled && alive.current) setLoading(false);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((n) => n + 1), []);
  const replace = useCallback((d: T) => { setData(d); setLoaded(true); setError(null); }, []);
  return { data, loading, error, loaded, reload, setData: replace };
}
