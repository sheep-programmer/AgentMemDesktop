import React from 'react';
import ReactDOM from 'react-dom/client';
// streamdown 内置 remark-math/rehype-katex 解析公式，但不自带样式；
// 缺这行数学公式会退化成 LaTeX 源码（如 $IC_{50}$ 直接显示原文）。
import 'katex/dist/katex.min.css';
import '@fontsource-variable/geist';
import App from './App';
import './styles/theme.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
