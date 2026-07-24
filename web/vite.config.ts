import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// 构建产物输出到 automind/static/dist，由 FastAPI 的 /static 挂载直接伺服；
// base 与挂载路径一致，资源带内容哈希（无需手动 cache-bust）。
export default defineConfig({
  plugins: [react()],
  base: '/static/dist/',
  build: {
    outDir: '../automind/static/dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1600,
    // 显式基线：兼容较旧的 WebView2/Edge 内核，避免个别机器因不支持的新语法
    // 直接白屏（对应"部分电脑安装后空白界面"）。es2020 覆盖桌面版目标内核。
    target: 'es2020',
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8765',
      '/v1': 'http://127.0.0.1:8765',
      '/manual': 'http://127.0.0.1:8765',
      '/ws': { target: 'ws://127.0.0.1:8765', ws: true },
    },
  },
});
