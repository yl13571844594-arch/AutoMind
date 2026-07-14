// 🧭 新手引导（首次打开自动显示；侧边栏可随时重看）
import { Button, Modal } from 'antd';
import { useState } from 'react';
import { useUi } from '../../store/ui';

const STEPS = [
  { icon: '🔑', title: '第 1 步 · 配置模型',
    body: <>点击左下角 <b>「⚙ 设置」→「🔑 API Keys」</b>，为你使用的模型提供商填入 API Key 并「测试连接」。<br /><br />支持 OpenAI / Claude / DeepSeek / Kimi / 智谱 / 豆包 / Gemini / 本地 Ollama，以及任意 OpenAI 兼容中转代理。</> },
  { icon: '🧭', title: '第 2 步 · 选择模式',
    body: <>顶部三个主模式：<br />• 💬 <b>对话</b> — 问答聊天，不动你的文件<br />• ⚙️ <b>工作</b> — 自动规划并执行任务（建项目、跑命令）<br />• 💻 <b>编程</b> — 读代码 → 改代码 → 跑测试闭环<br /><br />不确定选哪个？从 💬 对话开始最安全。<br /><br /><span className="hint-text">模型配置、主题、工作区、IDE 集成等都在左下角「⚙ 设置」菜单里。</span></> },
  { icon: '📚', title: '第 3 步 · 知识库与模板',
    body: <>侧边栏 <b>「📚 知识库」</b>可上传 PDF/Word/MD/TXT，对话时自动检索引用你的资料。<br /><br />不知道能做什么？点击顶部 <b>「📚 模板」</b>，内置 10 个常用模板（个人主页、修 Bug、写脚本、数据报告…），一键填入即可开跑。</> },
  { icon: '🛡️', title: '放心使用',
    body: <>• 每次工具调用都经过<b>风险评估与审批</b>，高危操作必须你确认<br />• Agent 改过的文件可在右栏 <b>「↩️ 文件改动」</b>一键撤销回滚<br />• 任务历史自动保存（📜），关掉浏览器也不会丢<br />• 全部数据只存在你自己的电脑上</> },
];

export default function TourModal() {
  const open = useUi((s) => s.modal) === 'tour';
  const close = useUi((s) => s.closeModal);
  const [idx, setIdx] = useState(0);

  const finish = () => {
    localStorage.setItem('automind_onboarded', '1');
    setIdx(0);
    close();
  };

  const s = STEPS[idx];
  return (
    <Modal open={open} onCancel={finish} footer={null} width={480}>
      <div style={{ textAlign: 'center', padding: '8px 4px' }}>
        <div style={{ fontSize: '2.6em' }}>{s.icon}</div>
        <h2 style={{ margin: '10px 0 4px' }}>{s.title}</h2>
        <div style={{ fontSize: '.9em', color: 'var(--text2)', lineHeight: 1.8, textAlign: 'left', margin: '14px 0' }}>{s.body}</div>
        <div style={{ display: 'flex', justifyContent: 'center', gap: 6, margin: '12px 0' }}>
          {STEPS.map((_, i) => (
            <span key={i} style={{ width: 8, height: 8, borderRadius: '50%', background: i === idx ? 'var(--accent)' : 'var(--bg3)' }} />
          ))}
        </div>
        <div style={{ display: 'flex', justifyContent: 'center', gap: 8 }}>
          {idx > 0 && <Button onClick={() => setIdx(idx - 1)}>上一步</Button>}
          {idx < STEPS.length - 1
            ? <Button type="primary" onClick={() => setIdx(idx + 1)}>下一步</Button>
            : <Button type="primary" onClick={finish}>开始使用 🚀</Button>}
        </div>
        <a style={{ display: 'inline-block', marginTop: 10, fontSize: '.78em', color: 'var(--text3)' }} onClick={finish}>跳过引导</a>
      </div>
    </Modal>
  );
}
