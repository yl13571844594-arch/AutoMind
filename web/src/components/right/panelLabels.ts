// v1.11.11 右栏展示映射：待办状态图标 / 子智能体状态中文与色级。
//
// 「面板」与「摘要」两个标签都要显示同一批数据，映射表只能有一份 ——
// 各写一套的话，"同一个状态在两页显示不同文字"这种低级不一致迟早出现。
export const TODO_ICON: Record<string, string> = {
  pending: '○', in_progress: '◐', completed: '✓',
};

export const SUBAGENT_LABEL: Record<string, string> = {
  running: '执行中', pending: '排队中', queued: '排队中',
  done: '完成', completed: '完成', ok: '完成',
  failed: '失败', error: '失败', cancelled: '已停止', stopped: '已停止', timeout: '超时',
};

/** 子智能体状态 → 卡片色级（running / done / failed / stopped）。 */
export const subagentTone = (status: string): 'running' | 'done' | 'failed' | 'stopped' =>
  ['running', 'pending', 'queued'].includes(status) ? 'running'
    : ['done', 'completed', 'ok'].includes(status) ? 'done'
      : ['failed', 'error', 'timeout'].includes(status) ? 'failed' : 'stopped';
