// React 渲染异常兜底。
//
// 没有 ErrorBoundary 时，任何一个组件在渲染中抛异常，React 18 会**卸载整棵树**
// —— 用户得到的是一整页纯白，控制台之外没有任何线索，连"回到上一个页面"都做不到。
// 一个统计视图里的 `undefined.map` 就能让整个工作台消失，正在跑的任务也看不见了。
//
// 这里分两层用：
//   1. 整个 App 外层 —— 最后的安全网；
//   2. 每个视图（右侧内容区）外层 —— 单个面板炸了，侧边栏与对话区照常可用，
//      用户能直接切到别的页面继续干活，而不是重开浏览器。
import { Component, type ErrorInfo, type ReactNode } from 'react';
import { Button } from 'antd';

interface Props {
  children: ReactNode;
  /** 出错区域的名字，显示给用户（如"统计分析"） */
  name?: string;
  /** 整页级别：给出刷新按钮；区域级别：给出重试按钮 */
  level?: 'page' | 'view';
  /** key 变化时自动复位（如切换视图后不该继续显示上一个页面的错误） */
  resetKey?: unknown;
}

interface State {
  error: Error | null;
  info: string;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, info: '' };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 控制台留完整栈供开发排查；界面上只给用户看得懂的部分
    console.error('[ErrorBoundary]', this.props.name || 'app', error, info.componentStack);
    this.setState({ info: (info.componentStack || '').slice(0, 1200) });
  }

  componentDidUpdate(prev: Props) {
    // 切到别的视图后，旧的错误不该继续糊在新页面上
    if (prev.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null, info: '' });
    }
  }

  private reset = () => this.setState({ error: null, info: '' });

  private copyDiag = () => {
    const { error, info } = this.state;
    const text = [
      `位置: ${this.props.name || 'app'}`,
      `错误: ${error?.name}: ${error?.message}`,
      `栈: ${error?.stack || '(无)'}`,
      `组件栈: ${info}`,
      `UA: ${navigator.userAgent}`,
      `URL: ${location.href}`,
    ].join('\n');
    navigator.clipboard?.writeText(text);
  };

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    const isPage = this.props.level === 'page';
    return (
      <div className={`err-boundary${isPage ? ' page' : ''}`}>
        <div className="eb-icon">💥</div>
        <div className="eb-title">
          {this.props.name ? `「${this.props.name}」出错了` : '界面出错了'}
        </div>
        <div className="eb-msg">
          {isPage
            ? '界面遇到了未预期的错误。你的任务与数据都在本地，刷新后即可继续。'
            : '这个面板没能正常显示，其它功能不受影响 —— 可以先切到别的页面继续用。'}
        </div>
        <code className="eb-detail">{error.name}: {error.message}</code>
        <div className="eb-acts">
          {isPage
            ? <Button type="primary" onClick={() => location.reload()}>刷新页面</Button>
            : <Button type="primary" onClick={this.reset}>重试</Button>}
          <Button onClick={this.copyDiag}>复制诊断信息</Button>
        </div>
        <div className="eb-hint">
          复制诊断信息后可提交 Issue，能帮我们快速定位。
        </div>
      </div>
    );
  }
}
