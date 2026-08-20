// 加载中 / 加载失败 / 有内容 —— 三态的统一呈现。
//
// 关键在于**把"加载中"和"没有数据"分开**：它们此前都渲染成"暂无数据"，
// 用户没法判断到底是还没取回来、还是接口挂了、还是真的一条都没有。
import type { ReactNode } from 'react';
import { Button, Skeleton } from 'antd';
import { ApiError } from '../../api/client';
import { EmptyState } from './Panel';

/** 加载失败面板：说清楚哪里失败、为什么、能怎么办。 */
export function ErrorPanel(
  { error, onRetry, what }: { error: Error; onRetry?: () => void; what?: string },
) {
  const api = error instanceof ApiError ? error : null;
  return (
    <div className="load-error">
      <div className="le-icon">⚠️</div>
      <div className="le-title">{what ? `${what}加载失败` : '加载失败'}</div>
      <div className="le-msg">{api ? api.friendly : error.message || '未知错误'}</div>
      {api && (
        <div className="le-meta">
          <code>{api.path}</code>{api.status ? ` · HTTP ${api.status}` : ' · 网络不可达'}
        </div>
      )}
      {onRetry && <Button size="small" onClick={onRetry} style={{ marginTop: 10 }}>重试</Button>}
    </div>
  );
}

/**
 * 包裹一次取数的三态渲染。
 *
 * - 首次加载（还没有任何数据）→ 骨架屏；
 * - 出错且没有可显示的数据 → 错误面板 + 重试；
 * - 出错但有旧数据 → 顶部挂一条错误条，下面继续显示旧数据
 *   （刷新失败不该把已经看到的内容清空）；
 * - 数据为空 → 调用方给的空态（真正的"没有数据"）。
 */
export function AsyncBoundary<T>(
  { state, what, isEmpty, empty, children, rows = 4 }: {
    state: { data: T | undefined; loading: boolean; error: Error | null; loaded: boolean; reload: () => void };
    what?: string;
    /** 判断"确实没有数据"；不给则不渲染空态 */
    isEmpty?: (d: T) => boolean;
    empty?: ReactNode;
    children: (d: T) => ReactNode;
    rows?: number;
  },
) {
  const { data, loading, error, loaded, reload } = state;

  if (loading && !loaded) {
    return (
      <div className="load-skeleton">
        <Skeleton active paragraph={{ rows }} />
      </div>
    );
  }
  if (error && !loaded) {
    return <ErrorPanel error={error} onRetry={reload} what={what} />;
  }
  if (data === undefined) {
    return <ErrorPanel error={error || new Error('没有取到数据')} onRetry={reload} what={what} />;
  }
  const isEmptyNow = isEmpty ? isEmpty(data) : false;
  return (
    <>
      {error && (
        <div className="load-stale">
          ⚠️ 刷新失败（{error instanceof ApiError ? error.friendly : error.message}），
          下方为上一次的数据。
          <Button size="small" type="link" onClick={reload}>重试</Button>
        </div>
      )}
      {isEmptyNow
        ? (empty ?? <EmptyState title="暂无数据" />)
        : children(data)}
    </>
  );
}
