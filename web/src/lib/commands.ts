// v1.11.2 输入框 / 指令目录。
//
// 四类指令，落地方式不同（由 ChatPanel.onPick 按 data.kind 分派）：
//   action  —— 立即执行的界面动作（新建会话、打开定时任务…），输入框里的指令文字被删掉；
//   prompt  —— 插入一段提示词模板（/explain → "请解释…"），光标停在后面接着写；
//   mode    —— 本条消息用某个模式发送（等同 ＋ 菜单里的「本条用…模式」）；
//   ref     —— 引用技能 / MCP 连接器 / 工具 / 专家，挂成 chip，服务端回执是否生效。
import type { SuggestItem } from '../components/chat/ComposerSuggest';
import { MODE_LABELS, type Mode } from '../store/app';
import { MODE_ICON } from './overrides';

export interface CommandCtx {
  featureOn: (k?: string) => boolean;
  modeFeature: Partial<Record<Mode, string>>;
  currentMode: Mode;
  planMode: boolean;
  skills: { name: string; emoji: string; type: string; description: string }[] | null;
  mcps: { name: string; connected: boolean; tools: number }[] | null;
  experts: { id: string; name: string; icon: string }[] | null;
  tools: { name: string; description: string; source: string }[] | null;
  picked: { skills: string[]; mcp: string[]; tools: string[] };
}

const ACTIONS: [string, string, string, string, string][] = [
  // [指令, 图标, 标题说明, 别名关键词, 动作]
  ['new', '＋', '新建会话', 'xinjian new session 新建', 'new'],
  ['clear', '🧹', '清空当前模式的对话', 'qingkong clear reset 清空', 'clear'],
  // v1.11.11：/compact 把当前会话上下文折叠压缩（释放窗口，任务不中断）。
  // 与 /clear 的区别要在标题里说清楚：一个只是腾地方，一个会丢掉对话内容。
  ['compact', '🧹', '压缩当前上下文（折叠历史，释放窗口；不清空对话）',
    'yasuo compact compress 压缩 上下文 折叠', 'compact'],
  ['plan', '📋', '进入 / 退出计划模式（先出计划，批准后执行）', 'jihua plan 计划', 'plan'],
  ['goal', '🎯', '目标交付：设置 / 查看长期目标', 'mubiao goal 目标', 'goal'],
  ['schedule', '⏰', '把后面写的内容设为定时任务', 'dingshi cron schedule timer 定时', 'schedule'],
  ['workflow', '▶', '运行工作流', 'gongzuoliu workflow run 工作流', 'workflow'],
  ['templates', '📑', '任务模板库', 'moban template 模板', 'templates'],
  ['browser', '🌐', '打开右栏浏览器预览', 'liulanqi browser preview 浏览器', 'browser'],
  ['model', '🖥', '模型配置', 'moxing model llm 模型', 'model'],
  ['keys', '🔑', 'API Keys', 'key apikey 密钥', 'apikeys'],
  ['settings', '⚙', '通用设置', 'shezhi settings config 设置', 'general'],
  ['workspace', '🗂', '工作区管理', 'gongzuoqu workspace project 工作区', 'workspaces'],
  ['theme', '🌓', '切换深色 / 浅色主题', 'zhuti theme dark light 主题', 'theme'],
  ['upgrade', '⭐', '升级专业版 / 企业版、许可证', 'shengji license upgrade 升级 许可证', 'license'],
  ['help', '⌨', '键盘快捷键与指令说明', 'bangzhu help shortcuts 帮助', 'shortcuts'],
];

const PROMPTS: [string, string, string, string, string][] = [
  ['explain', '💡', '解释代码或内容', 'jieshi explain 解释',
    '请解释下面内容的作用、关键逻辑与潜在问题：\n'],
  ['fix', '🩹', '定位并修复问题', 'xiufu fix bug debug 修复 报错',
    '请定位并修复这个问题：先说明根因，再给出修改并验证：\n'],
  ['test', '🧪', '编写 / 补充单元测试', 'ceshi test unittest 测试',
    '请为下面的代码编写单元测试，覆盖正常路径、边界条件与错误处理，并运行确认通过：\n'],
  ['review', '🔍', '代码审查', 'shencha review cr 审查',
    '请审查下面的代码：指出 bug、安全隐患、性能问题与可读性问题，按严重程度排序并给出修改建议：\n'],
  ['refactor', '🧱', '重构（行为不变）', 'chonggou refactor 重构',
    '请在不改变外部行为的前提下重构下面的代码，提升可读性与可维护性，并说明每处改动的理由：\n'],
  ['summarize', '📝', '总结要点', 'zongjie summary tldr 总结',
    '请总结下面内容的要点（结论在前，分条列出关键信息与待办）：\n'],
  ['translate', '🌐', '翻译（中英互译）', 'fanyi translate 翻译',
    '请把下面内容翻译成地道的中文（若原文是中文则译为英文），保留术语与格式：\n'],
  ['doc', '📖', '编写文档 / 注释', 'wendang doc readme 文档 注释',
    '请为下面的代码编写清晰的文档（用途、参数、返回值、示例），并补充必要的注释：\n'],
];

export function buildCommands(ctx: CommandCtx): SuggestItem[] {
  const out: SuggestItem[] = [];
  for (const [cmd, icon, title, kw, action] of ACTIONS) {
    let desc = title;
    if (cmd === 'plan') desc = ctx.planMode ? '退出计划模式' : title;
    const locked = cmd === 'schedule' && !ctx.featureOn('scheduler');
    out.push({
      key: 'a:' + cmd, group: '操作', icon, title: '/' + cmd, desc: desc + (locked ? ' 🔒' : ''),
      keywords: kw, hint: '执行', data: { kind: 'action', action },
    });
  }
  for (const m of ['chat', 'work', 'coding', 'multi', 'loop'] as Mode[]) {
    const locked = !ctx.featureOn(ctx.modeFeature[m]);
    out.push({
      key: 'm:' + m, group: '本条模式', icon: MODE_ICON[m], title: '/' + m,
      desc: `本条用「${MODE_LABELS[m]}」模式发送${m === ctx.currentMode ? '（当前）' : ''}${locked ? ' 🔒' : ''}`,
      keywords: `${MODE_LABELS[m]} mode`, disabled: locked, data: { kind: 'mode', mode: m },
    });
  }
  for (const [cmd, icon, title, kw, text] of PROMPTS) {
    out.push({
      key: 'p:' + cmd, group: '提示词', icon, title: '/' + cmd, desc: title,
      keywords: kw, hint: '插入', data: { kind: 'prompt', text },
    });
  }
  const loading = (group: string, k: string): SuggestItem =>
    ({ key: k, group, icon: '⏳', title: '加载中…', disabled: true });
  // 技能
  if (ctx.skills === null) out.push(loading('技能', 's:loading'));
  for (const s of ctx.skills || []) {
    const on = ctx.picked.skills.includes(s.name);
    out.push({
      key: 's:' + s.name, group: '技能', icon: s.emoji || '🧩', title: '/' + s.name,
      desc: (on ? '✓ 已引用 · ' : '') + (s.description || (s.type === 'markdown' ? '指令型技能' : '可执行技能')),
      keywords: `skill 技能 ${s.type}`, hint: s.type === 'markdown' ? '指令' : '可执行',
      data: { kind: 'ref', ref: 'skill', name: s.name },
    });
  }
  // MCP 连接器
  if (ctx.mcps === null) out.push(loading('MCP 连接器', 'c:loading'));
  for (const c of ctx.mcps || []) {
    const on = ctx.picked.mcp.includes(c.name);
    out.push({
      key: 'c:' + c.name, group: 'MCP 连接器', icon: '🔌', title: '/' + c.name,
      desc: (on ? '✓ 已引用 · ' : '') + (c.connected ? `${c.tools} 个工具，本条必定下发` : '未连接'),
      keywords: 'mcp 连接器 connector', disabled: !c.connected,
      data: { kind: 'ref', ref: 'mcp', name: c.name },
    });
  }
  // 插件 / MCP 单个工具 / 内置工具
  if (ctx.tools === null) out.push(loading('工具与插件', 't:loading'));
  const SRC: Record<string, [string, string]> = {
    plugin: ['🧷', '插件'], mcp: ['🔌', 'MCP'], builtin: ['🔧', '内置'],
  };
  const order = (s: string) => (s === 'plugin' ? 0 : s === 'mcp' ? 1 : 2);
  for (const t of [...(ctx.tools || [])].sort((a, b) => order(a.source) - order(b.source))) {
    const [icon, label] = SRC[t.source] || SRC.builtin;
    const on = ctx.picked.tools.includes(t.name);
    out.push({
      key: 't:' + t.name, group: '工具与插件', icon, title: '/' + t.name,
      desc: (on ? '✓ 已指定 · ' : '') + `${label} · ${(t.description || '').split('\n')[0].slice(0, 70)}`,
      keywords: `tool 工具 ${label} plugin`, data: { kind: 'ref', ref: 'tool', name: t.name },
    });
  }
  // 专家
  for (const x of ctx.experts || []) {
    out.push({
      key: 'e:' + x.id, group: '专家', icon: x.icon || '🎓', title: '/' + x.name,
      desc: '本条挂这位专家', keywords: 'expert 专家', data: { kind: 'ref', ref: 'expert', id: x.id, name: x.name },
    });
  }
  return out;
}
