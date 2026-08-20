import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { ErrorBoundary } from './components/ui/ErrorBoundary';
import './global.css';

// 最外层兜底：App 自身（Provider / 布局 / 全局 store 初始化）出错时，
// 没有它就是**一整页纯白** —— 用户既看不到原因，也没有可点的东西。
// 视图级边界在 App.tsx 内部，负责把单个面板的崩溃限制在该面板内。
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary level="page">
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
