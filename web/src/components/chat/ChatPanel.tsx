// 对话工作台：消息流（各类气泡）+ 输入区（附件/语音/发送/停止）。
// 首次进入对话模式时从服务端恢复历史；其它模式恢复本地持久化内容。
import { App } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { apiGet, apiPost } from '../../api/client';
import { chatSid, MODE_LABELS, useApp, type Mode } from '../../store/app';
import { uid, useChat, type ChatItem } from '../../store/chat';
import { usePanel } from '../../store/panel';
import { useUi } from '../../store/ui';
import { sendRun, sendStop, wsReady } from '../../ws';
import {
  ErrorBubble, ExecBubble, LoopBubble, MsgBubble, MultiBubble, ResumeBubble,
  StreamBubble, TypingBubble, WelcomeBubble,
} from './Bubbles';
import { TEMPLATES } from '../../lib/templates';
import TaskProgress from './TaskProgress';

// zustand v5 的 getSnapshot 需返回稳定引用：空列表复用同一常量，
// 否则每次渲染生成新数组会触发 React #185（无限重渲染）。
const EMPTY_ITEMS: ChatItem[] = [];

export default function ChatPanel() {
  const { message } = App.useApp();
  const mode = useApp((s) => s.mode);
  const running = useApp((s) => s.running);
  const items = useChat((s) => s.messages[mode] ?? EMPTY_ITEMS);
  const pendingImages = useChat((s) => s.pendingImages);
  const modalOpen = useUi((s) => s.modal !== null || s.preview !== null);
  const wsSuffix = useApp((s) => s.wsSuffix);
  const listRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [recognizing, setRecognizing] = useState(false);
  const recRef = useRef<any>(null);
  const saveTimer = useRef<number | undefined>(undefined);
  // 只在挂载时读一次草稿：textarea 是非受控的，若改成订阅 inputDraft，
  // 每次按键都会重渲整个消息列表，长会话下打字会明显卡顿。
  const draft0 = useRef(useChat.getState().inputDraft).current;

  // 草稿保存：输入停顿 400ms 落一次盘，避免每个按键都写 localStorage。
  const saveDraft = (immediate = false) => {
    window.clearTimeout(saveTimer.current);
    const flush = () => useChat.getState().setInputDraft(taRef.current?.value || '');
    if (immediate) flush();
    else saveTimer.current = window.setTimeout(flush, 400);
  };

  // 关窗/切标签/组件卸载时立刻补存一次 —— 否则最后 400ms 内敲的字会丢，
  // 而"最后敲的那几个字"恰恰是用户最在意的部分。
  useEffect(() => {
    const flush = () => saveDraft(true);
    window.addEventListener('beforeunload', flush);
    document.addEventListener('visibilitychange', flush);
    return () => {
      window.removeEventListener('beforeunload', flush);
      document.removeEventListener('visibilitychange', flush);
      flush();
      window.clearTimeout(saveTimer.current);
    };
  }, []);

  // 切工作区时把输入框换成该工作区自己的草稿。
  // textarea 是非受控的，`defaultValue` 只在挂载时生效，而 v1.4.0 起 ChatPanel
  // 常驻挂载不再重建 —— 不手动同步的话，切过去看到的还是上一个工作区的草稿，
  // 接着一打字又会被存到新工作区名下。
  // 先撤掉待触发的防抖保存：它读的是旧文本，而落盘的 key 已经是新工作区了。
  useEffect(() => {
    window.clearTimeout(saveTimer.current);
    if (taRef.current) {
      taRef.current.value = useChat.getState().inputDraft;
      taRef.current.style.height = 'auto';
    }
  }, [wsSuffix]);

  // 打开即聚焦，省掉"先点一下输入框"这一步；任务跑完后也自动交还焦点。
  // 有弹窗时不抢焦点（否则弹窗里的输入框会被顶掉）。
  useEffect(() => {
    if (modalOpen || running) return;
    const t = window.setTimeout(() => taRef.current?.focus({ preventScroll: true }), 60);
    return () => window.clearTimeout(t);
  }, [mode, running, modalOpen]);

  // 对话模式且本地无记录 → 从服务端恢复历史
  useEffect(() => {
    const st = useChat.getState();
    if (mode === 'chat' && (st.messages.chat || []).length === 0) {
      apiGet(`/chat/history?session_id=${encodeURIComponent(chatSid())}`).then((h) => {
        const msgs = (h.messages || []).filter((m: any) => m.role === 'user' || m.role === 'assistant');
        if (msgs.length) {
          st.setMessages('chat', msgs.map((m: any): ChatItem => ({
            kind: 'msg', id: uid(), role: m.role === 'user' ? 'user' : 'agent',
            md: typeof m.content === 'string' ? m.content
              : (Array.isArray(m.content) ? m.content.filter((p: any) => p.type === 'text').map((p: any) => p.text).join('') : ''),
          })));
        }
      }).catch(() => {});
    }
  }, [mode]);

  // 自动滚动到底
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [items]);

  // override 用于续跑/重试这类"不是从输入框来"的发送。
  // 模式一律从 store 现取：续跑可能刚切过模式，闭包里的 mode 已经是旧值。
  const send = async (override?: string) => {
    if (useApp.getState().running) return;
    const st = useChat.getState();
    const cur = useApp.getState().mode;
    const text = (override ?? taRef.current?.value ?? '').trim();
    const images = st.pendingImages.slice();
    if (!text && !images.length) return;
    if (!wsReady()) { message.error('未连接到服务器，请稍候重试'); return; }
    if (override === undefined && taRef.current) {
      taRef.current.value = '';
      taRef.current.style.height = 'auto';
    }
    window.clearTimeout(saveTimer.current);   // 别让待触发的保存把草稿又写回来
    st.setInputDraft('');
    st.setPendingImages([]);
    st.append(cur, { kind: 'msg', id: uid(), role: 'user', md: text, images });
    st.setTaskMode(cur);
    st.setLastTask({ text, mode: cur });
    useApp.getState().setRunning(true);
    st.append(cur, { kind: 'typing', id: uid() });
    sendRun(text, images);
  };

  // 从失败卡片重跑/续跑：任务原文取自卡片自身，重启应用后依然有效。
  //
  // wrap=true 并非"断点续传"——后端没有把执行进度回传给前端，这里能做的只是在
  // 原任务前加一段提示，让模型自己先去核对现状。措辞用"尽量/若已完成则跳过"
  // 而不是"不要重做"，因为能不能真跳过取决于模型的核对结果，不该承诺死。
  const runAgain = async (text: string, m: Mode, wrap: boolean) => {
    if (!text) { message.error('没有可继续的任务'); return; }
    if (useApp.getState().running) { message.error('任务正在执行中'); return; }
    if (m !== useApp.getState().mode) await useApp.getState().setMode(m);
    await send(wrap
      ? '下面这个任务此前执行到一半中断了。请先检查当前的实际进度（已存在的文件、'
        + '已完成的改动），已经做好的部分就不必重做，从尚未完成的地方接着做：\n'
        + text
      : text);
  };

  // 编辑后重发：先砍掉这条及其之后的消息（基于旧问题的回答已经失效，
  // 留着会让上下文自相矛盾），再把改好的内容作为一条新消息发出去。
  const resendEdited = async (id: string, text: string) => {
    if (useApp.getState().running) { message.error('任务正在执行中，请先停止'); return; }
    useChat.getState().truncateFrom(mode, id);
    await send(text);
  };

  const resume = async (m: Mode) => {
    const last = useChat.getState().lastTask;
    if (!last || !last.text) { message.error('没有可继续的任务'); return; }
    await runAgain(last.text, last.mode, true);
  };

  const pickImages = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    files.forEach((f) => {
      if (!f.type.startsWith('image/')) return;
      if (f.size > 8 * 1024 * 1024) { message.error('图片不能超过 8MB'); return; }
      const reader = new FileReader();
      reader.onload = (ev) => {
        const st = useChat.getState();
        st.setPendingImages([...st.pendingImages, ev.target?.result as string]);
      };
      reader.readAsDataURL(f);
    });
    e.target.value = '';
  };

  const toggleVoice = () => {
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR) { message.error('当前浏览器不支持语音识别，请使用 Chrome 或 Edge'); return; }
    if (recognizing) { recRef.current?.stop(); return; }
    const rec = new SR();
    rec.lang = 'zh-CN';
    rec.interimResults = true;
    const base = taRef.current?.value || '';
    rec.onstart = () => { setRecognizing(true); message.info('正在聆听...'); };
    rec.onerror = (ev: any) => message.error('语音识别失败: ' + ev.error);
    rec.onend = () => setRecognizing(false);
    rec.onresult = (ev: any) => {
      let txt = '';
      for (let i = 0; i < ev.results.length; i++) txt += ev.results[i][0].transcript;
      if (taRef.current) taRef.current.value = (base ? base + ' ' : '') + txt;
      saveDraft();   // 语音听写出来的内容同样算草稿，别在切走时丢掉
    };
    recRef.current = rec;
    rec.start();
  };

  const useTemplate = async (i: number) => {
    const t = TEMPLATES[i];
    if (!t) return;
    if (t.mode !== mode) await useApp.getState().setMode(t.mode as Mode);
    if (taRef.current) { taRef.current.value = t.prompt; taRef.current.focus(); saveDraft(true); }
    message.info('模板已填入，补充细节后按 Enter 发送');
  };

  const showWelcome = items.length === 0;

  return (
    // minHeight: 0 不能省 —— flex 子项的自动最小尺寸是 auto，即"不小于内容高度"。
    // 少了它，消息一多本容器就被内容撑到上万像素、超出父容器，.messages 拿不到
    // 受限高度因而永远不触发 overflow-y:auto，输入框被顶到视口外彻底看不见。
    // （minWidth: 0 是同一条规则的横向版本，早已加上；纵向这条此前漏了。）
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0, minHeight: 0 }}>
      <div className="messages" ref={listRef}>
        {showWelcome && (
          <WelcomeBubble onTemplate={useTemplate} onAllTemplates={() => useUi.getState().openModal('templates')} />
        )}
        {items.map((item) => {
          switch (item.kind) {
            case 'msg': return (
              <MsgBubble
                key={item.id}
                item={item}
                onDelete={(id) => useChat.getState().remove(mode, id)}
                onResend={resendEdited}
              />
            );
            case 'stream': return <StreamBubble key={item.id} item={item} />;
            case 'typing': return <TypingBubble key={item.id} />;
            case 'exec': return <ExecBubble key={item.id} item={item} />;
            case 'multi': return <MultiBubble key={item.id} item={item} />;
            case 'loop': return <LoopBubble key={item.id} item={item} />;
            case 'resume': return <ResumeBubble key={item.id} item={item} onResume={() => resume(mode)} />;
            case 'error': return (
              <ErrorBubble
                key={item.id}
                item={item}
                onResume={(it) => runAgain(it.task || '', it.taskMode || mode, true)}
                onRetry={(it) => runAgain(it.task || '', it.taskMode || mode, false)}
              />
            );
            default: return null;
          }
        })}
      </div>

      {pendingImages.length > 0 && (
        <div style={{ display: 'flex', gap: 8, padding: '6px 18px', flexWrap: 'wrap' }}>
          {pendingImages.map((u, i) => (
            <div key={i} style={{ position: 'relative' }}>
              <img src={u} style={{ height: 56, borderRadius: 8, border: '1px solid var(--border)' }} />
              <button
                onClick={() => {
                  const st = useChat.getState();
                  st.setPendingImages(st.pendingImages.filter((_, k) => k !== i));
                }}
                style={{
                  position: 'absolute', top: -6, right: -6, width: 18, height: 18, borderRadius: '50%',
                  border: 'none', background: 'var(--red)', color: '#fff', cursor: 'pointer', fontSize: 10,
                }}
              >✕</button>
            </div>
          ))}
        </div>
      )}

      <TaskProgress />

      <div className="input-bar">
       <div className="input-inner">
        <button className="tpl-chip" title="添加图片（多模态）" onClick={() => fileRef.current?.click()}>📎</button>
        <button
          className="tpl-chip"
          title="语音输入（麦克风）"
          style={recognizing ? { borderColor: 'var(--red)', color: 'var(--red)' } : {}}
          onClick={toggleVoice}
        >🎤</button>
        <input ref={fileRef} type="file" accept="image/*" multiple style={{ display: 'none' }} onChange={pickImages} />
        <textarea
          ref={taRef}
          defaultValue={draft0}
          disabled={running}
          rows={1}
          placeholder={({
            chat: '输入消息，Enter 发送，Shift+Enter 换行...',
            work: '描述你想完成的任务，AutoMind 会自主规划并执行...',
            coding: '描述编程需求（创建/修复/重构/测试），AutoMind 会读写代码并运行...',
            multi: '描述一个较复杂的任务，多个智能体将分工协作完成...',
            loop: '描述一个需要反复迭代直到达标的目标，系统将自主循环修正...',
          } as Record<Mode, string>)[mode]}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
          }}
          onInput={(e) => {
            const el = e.currentTarget;
            el.style.height = 'auto';
            el.style.height = Math.min(el.scrollHeight, 180) + 'px';
            saveDraft();
          }}
          onBlur={() => saveDraft(true)}
          style={{
            flex: 1, resize: 'none', maxHeight: 180, padding: '10px 14px',
            border: '1px solid var(--border)', borderRadius: 12, outline: 'none',
            background: 'var(--bg0)', color: 'var(--text)', fontFamily: 'var(--font)',
            fontSize: '.92em', lineHeight: 1.6,
          }}
        />
        {/* 不能直接 onClick={send}：React 会把 MouseEvent 当成 override 参数传进去 */}
        {!running ? (
          <button onClick={() => send()} title="发送 (Enter)" className="send-btn">▶</button>
        ) : (
          <button onClick={sendStop} title="停止" className="send-btn stop">■</button>
        )}
       </div>
      </div>
    </div>
  );
}
export { MODE_LABELS, usePanel };
