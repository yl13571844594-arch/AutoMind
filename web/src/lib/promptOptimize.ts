// v1.11.20 输入问题优化（prompt-opt）—— 把"帮我看看"改写成能一次做对的任务。
//
// 为什么单独一个 lib 而不是塞在 ChatPanel 里：
//   1. 服务端（automind/core/prompt_optimizer.py）优先用模型改写、失败时本地规则
//      兜底；前端也要一个**近似同源**的模糊判据，用来决定"什么时候该提示用户点 ✨"。
//      两边阈值放在一起，才不会出现"按钮不亮但服务端说模糊"的漂移；
//   2. 这个判断要在每次输入时跑，不能顺手塞进组件状态里（那会重渲整个消息列表）。
import { apiPost } from '../api/client';

/** 服务端一次优化的结果（字段与 prompt_optimizer.OptimizedPrompt.to_dict() 一致）。 */
export interface OptimizeResult {
  /** 用户原话（已去首尾空白） */
  original: string;
  /** 优化后的问题；清晰问题会与 original 相同 */
  optimized: string;
  /** 本次是否真的改写了 */
  changed: boolean;
  /** 原问题是否已经足够清楚（前端据此提示"可直接发送"） */
  clear_enough: boolean;
  /** 改写来源：llm = 模型改写；rule = 本地规则兜底 */
  by: 'llm' | 'rule';
  /** 改动点（最多 5 条，每条 200 字以内） */
  notes: string[];
  /** 需要用户确认的信息（最多 3 条） */
  questions: string[];
}

/** 模糊判据阈值：与服务端 MIN_MEANINGFUL_CHARS / SHORT_PROMPT_CHARS 保持一致。 */
const MIN_MEANINGFUL_CHARS = 12;
const SHORT_PROMPT_CHARS = 40;

/**
 * 一次优化请求的超时（毫秒）。
 *
 * 服务端对模型的硬超时是 30 秒，随后还要走规则兜底并回传；客户端留 45 秒，
 * 保证"服务端先超时降级"而不是"客户端先掐断"，用户拿到的至少是规则版本。
 */
export const OPTIMIZE_TIMEOUT_MS = 45000;

/** 纯泛化短语（与服务端 _VAGUE_PHRASES 对齐）。 */
const VAGUE_PHRASES = [
  '帮我弄一下', '帮我弄下', '帮我搞一下', '帮我搞下', '帮我处理一下',
  '帮忙看看', '帮我看看', '帮我看下', '帮我查下', '帮我改下', '帮我看一下',
  '看看这个', '看下这个', '看看那个', '看看这段', '看一下这个',
  '优化一下', '优化下', '改一下', '改改', '修一下', '修修',
  '继续', '接着来', '继续吧', '怎么办', '咋办', '怎么弄', '怎么搞',
  '这是什么', '这是什么意思', '这是啥', '不行', '报错了', '有bug', '跑不通',
  '没反应', '不能用', '有点问题', '处理一下', '搞一下', '弄一下', '搞不定',
  '试试', '看看吧', '看看', '分析一下',
];

/** 具体对象（路径 / URL / 数字 / 扩展名）—— 有它们就不算"纯泛化"。 */
const STRUCTURE_PATTERNS: RegExp[] = [
  /https?:\/\/\S+/i,                        // URL
  /\bwww\.[\w.-]+/i,                        // 裸域名
  /[A-Za-z]:[\\/][^\s]*/,                   // Windows 绝对路径
  /(?:^|\s)(?:~|\.{1,2})?[\\/][\w./\\-]+/,  // /usr/...、./src/...
  /[\w.-]+[\\/][\w./\\-]+/,                 // src/app.py、a/b
  /\.[A-Za-z][A-Za-z0-9]{0,7}\b/,           // 文件扩展名 main.py
  /\d/,                                     // 数字（版本/端口/行号/数量）
];

/** 句子标点：出现标点通常意味着用户已经在分句组织信息。 */
const PUNCT_STRIP_RE = /[，。！？；：、,.!?;:'"()（）[\]{}<>《》「」『』【】…—~～`]/g;

/**
 * 本地启发式：这句话是否需要"优化"。
 *
 * 判据与服务端一致（见 `automind/core/prompt_optimizer.py:looks_vague`）：
 *   1. 去掉空白后不超过 12 个字；
 *   2. 去掉标点后是纯泛化短语；
 *   3. 不足 40 字、没有标点、也没有路径 / URL / 数字 / 扩展名这类具体对象。
 *
 * 只负责给按钮加一点提示（hint），**不会自动改写、不阻塞发送** ——
 * 判错的代价必须足够低。
 */
export function looksVague(text: string): boolean {
  const raw = (text || '').trim();
  // 按 Unicode 码点计数：中文与 emoji 在 UTF-16 里长度不同，直接 .length 会漂移。
  const core = raw.replace(/\s+/g, '');
  if (Array.from(core).length <= MIN_MEANINGFUL_CHARS) return true;
  const stripped = core.replace(PUNCT_STRIP_RE, '');
  if (VAGUE_PHRASES.includes(stripped)) return true;
  // stripped === core 即"没有标点"；再排除路径/URL/数字/扩展名这类具体对象。
  return (
    Array.from(core).length < SHORT_PROMPT_CHARS
    && stripped === core
    && !STRUCTURE_PATTERNS.some((p) => p.test(core))
  );
}

/**
 * 调服务端优化一次问题。
 *
 * `interaction` 传当前模式（chat/work/coding/multi/loop）：只作为模型改写的
 * 上下文提示，不改变接口结构。失败会抛 ApiError（由调用方 toast）。
 * 后端本身有规则兜底，正常路径几乎不会失败 —— 失败基本是网络 / 服务未启动。
 */
export async function optimizePrompt(text: string, interaction: string): Promise<OptimizeResult> {
  return apiPost<OptimizeResult>(
    '/prompt/optimize',
    { text, interaction },
    OPTIMIZE_TIMEOUT_MS,
  );
}
