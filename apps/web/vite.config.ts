/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'node:path';
import fs from 'node:fs';
import type { Plugin } from 'vite';

/**
 * pdf.js 运行时按需去取的静态资源：CMap（中日韩字体没嵌入的 PDF 靠它出字）、
 * 标准 14 字体、ICC 配置、JPEG2000 / JBIG2 解码用的 wasm。
 *
 * pdf.js 只认「目录 URL」，打包器没法把它们当模块处理；拷进 public/ 又会把 4MB
 * 二进制塞进仓库。这里开发时直接从 node_modules 里读，构建时原样拷到 dist/pdfjs/，
 * 只有真打开 PDF、真用到某个字体时浏览器才会去取。
 * quickjs 是给 PDF 内嵌脚本用的，阅读器不跑脚本，不拷。
 */
function pdfjsAssets(): Plugin {
  const root = path.resolve(__dirname, 'node_modules/pdfjs-dist');
  const dirs = ['cmaps', 'standard_fonts', 'iccs', 'wasm'];
  const wanted = (name: string) => !name.startsWith('quickjs') && !name.startsWith('LICENSE');
  return {
    name: 'agentmem:pdfjs-assets',
    configureServer(server) {
      server.middlewares.use('/pdfjs', (req, res, next) => {
        const [dir, file, ...rest] = (req.url ?? '').split('?')[0].split('/').filter(Boolean);
        // 只放行白名单目录下的单层文件名，不给 ../ 之类留口子
        if (!dir || !file || rest.length > 0 || !dirs.includes(dir) || !wanted(file)) return next();
        const target = path.join(root, dir, path.basename(decodeURIComponent(file)));
        if (!fs.existsSync(target)) return next();
        fs.createReadStream(target).pipe(res);
      });
    },
    generateBundle() {
      for (const dir of dirs) {
        for (const file of fs.readdirSync(path.join(root, dir)).filter(wanted)) {
          this.emitFile({
            type: 'asset',
            fileName: `pdfjs/${dir}/${file}`,
            source: fs.readFileSync(path.join(root, dir, file)),
          });
        }
      }
    },
  };
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    pdfjsAssets(),
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  // 单元测试只跑纯逻辑与小组件，用 jsdom 就够；真实浏览器那一档交给 scripts/check-contrast.mjs
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        // 指向别的后端：调试、对拍、跑验证脚本时不必去动用户正在用的那个实例
        target: process.env.AGENTMEM_API_TARGET ?? 'http://127.0.0.1:8765',
        changeOrigin: true,
      },
    },
  },
});
