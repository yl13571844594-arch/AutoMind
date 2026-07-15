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
  ExecBubble, LoopBubble, MsgBubble, MultiBubble, ResumeBubble, StreamBubble,
  TypingBubble, WelcomeBubble,
} from './Bubbles';
import { TEMPLATES } from '../../lib/templates';

// zustand v5 的 getSnapshot 需返回稳定引用：空列表复用同一常量，
// 否则每次渲染生成新数组会触发 React #185（无限重渲染）。
const EMPTY_ITEMS: ChatItem[] = [];

export default function ChatPanel() {
  const { message } = App.useApp();
  const mode = useApp((s) => s.mode);
  const running = useApp((s) => s.running);
  const items = useChat((s) => s.messages[mode] ?? EMPTY_ITEMS);
  const pendingImages = useChat((s) => s.pendingImages);
  const draft = useChat((s) => s.inputDraft);
  const listRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [recognizing, setRecognizing] = useState(false);
  const recRef = useRef<any>(null);

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

  const send = async () => {
    if (running) return;
    const st = useChat.getState();
    const text = (taRef.current?.value || '').trim();
    const images = st.pendingImages.slice();
    if (!text && !images.length) return;
    if (!wsReady()) { message.error('未连接到服务器，请稍候重试'); return; }
    if (taRef.current) { taRef.current.value = ''; taRef.current.style.height = 'auto'; }
    st.setInputDraft('');
    st.setPendingImages([]);
    st.append(mode, { kind: 'msg', id: uid(), role: 'user', md: text, images });
    st.setTaskMode(mode);
    st.setLastTask({ text, mode });
    useApp.getState().setRunning(true);
    st.append(mode, { kind: 'typing', id: uid() });
    sendRun(text, images);
  };

  const resume = async (m: Mode) => {
    const last = useChat.getState().lastTask;
    if (!last || !last.text) { message.error('没有可继续的任务'); return; }
    if (running) { message.error('任务正在执行中'); return; }
    if (last.mode !== mode) await useApp.getState().setMode(last.mode);
    if (taRef.current) {
      taRef.current.value = `继续完成此前被中断的任务（不要重做已完成的部分，先检查现状再从中断处继续）：\n${last.text}`;
    }
    setTimeout(send, 50);
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
    };
    recRef.current = rec;
    rec.start();
  };

  const useTemplate = async (i: number) => {
    const t = TEMPLATES[i];
    if (!t) return;
    if (t.mode !== mode) await useApp.getState().setMode(t.mode as Mode);
    if (taRef.current) { taRef.current.value = t.prompt; taRef.current.focus(); }
    message.info('模板已填入，补充细节后按 Enter 发送');
  };

  const showWelcome = items.length === 0;

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
      <div className="messages" ref={listRef}>
        {showWelcome && (
          <WelcomeBubble onTemplate={useTemplate} onAllTemplates={() => useUi.getState().openModal('templates')} />
        )}
        {items.map((item) => {
          switch (item.kind) {
            case 'msg': return <MsgBubble key={item.id} item={item} />;
            case 'stream': return <StreamBubble key={item.id} item={item} />;
            case 'typing': return <TypingBubble key={item.id} />;
            case 'exec': return <ExecBubble key={item.id} item={item} />;
            case 'multi': return <MultiBubble key={item.id} item={item} />;
            case 'loop': return <LoopBubble key={item.id} item={item} />;
            case 'resume': return <ResumeBubble key={item.id} item={item} onResume={() => resume(mode)} />;
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
          defaultValue={draft}
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
          }}
          style={{
            flex: 1, resize: 'none', maxHeight: 180, padding: '10px 14px',
            border: '1px solid var(--border)', borderRadius: 12, outline: 'none',
            background: 'var(--bg0)', color: 'var(--text)', fontFamily: 'var(--font)',
            fontSize: '.92em', lineHeight: 1.6,
          }}
        />
        {!running ? (
          <button onClick={send} title="发送 (Enter)" className="send-btn">▶</button>
        ) : (
          <button onClick={sendStop} title="停止" className="send-btn stop">■</button>
        )}
       </div>
      </div>
    </div>
  );
}
export { MODE_LABELS, usePanel };
