// 让 tsc 认识组件内 `import './ModelPicker.css'`（Vite 构建时处理样式，
// 这里只需要一个类型声明；没有它 `tsc --noEmit` 会报 TS2307 找不到模块）。
declare module '*.css';
