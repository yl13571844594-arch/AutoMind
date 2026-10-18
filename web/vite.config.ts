import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// 构建产物输出到 automind/static/dist，由 FastAPI 的 /static 挂载直接伺服；
// base 与挂载路径一致，资源带内容哈希（无需手动 cache-bust）。
export default defineConfig({
  plugins: [react()],
  resolve: {
    // v1.11.3：App.tsx 用 @ant-design/cssinjs 的 StyleProvider 切换 antd 兼容样式。
    // 它只对**同一份** cssinjs 生效 —— 依赖树里若有两份（npm / pnpm 布局不同时会
    // 出现），StyleProvider 就成了另一个 React context，兼容模式静默失效。
    dedupe: ['@ant-design/cssinjs', 'react', 'react-dom'],
  },
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
    // CSS 单独给浏览器基线（v1.11.1）：只写 es2017 时 esbuild 不知道要兼容哪些
    // 内核，CSS 原样输出 —— `inset` 在 Chromium < 87 被整条忽略（遮罩层错位），
    // `backdrop-filter` 在 Safari < 18 需要 -webkit- 前缀（毛玻璃失效、文字压在
    // 半透明底上看不清）。给出具体内核后 esbuild 会自动降级与补前缀。
    cssTarget: ['chrome61', 'edge79', 'firefox68', 'safari12'],
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
