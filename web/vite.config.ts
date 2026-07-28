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
    // 直接白屏（对应"部分电脑安装后空白界面"）。
    // es2020 → es2017：可选链 `?.`/空值合并 `??` 需 Chromium 80+，而企业
    // 机器上被组策略钉住的 WebView2 可能停留在更早的内核；es2017 只要求
    // Chromium 55+，覆盖面显著更广，产物体积代价可忽略。
    target: 'es2017',
    // modulepreload polyfill：老内核不支持 <link rel="modulepreload"> 时
    // 由 polyfill 预取，避免分包被跳过导致的加载失败。
    modulePreload: { polyfill: true },
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
