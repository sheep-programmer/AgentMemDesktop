// v2 合成素材。世界坐标（CSS 像素）：1680×945，应用窗口 1440×810 摆在 (120, 67.5)。
// 背景与阴影按 2x 渲染，镜头推近时依然清晰；字幕、片头片尾按屏幕坐标 1920×1080 渲染。
import { chromium } from 'playwright';
import fs from 'node:fs/promises';

const DIR = process.env.PROMO_DIR ?? '/tmp/agentmem-promo';
const OUT = `${DIR}/assets`;
await fs.mkdir(OUT, { recursive: true });
const LOGO = await fs.readFile(new URL('../../public/logo.svg', import.meta.url), 'utf8');
const WORLD = { w: 1680, h: 945, win: { x: 120, y: 67.5, w: 1440, h: 810, r: 14 } };

export const CAPTIONS = {
  c1: ['01', '资料导入', '把资料交给它', 'PDF、Word、Markdown、网页，拖进来就能用'],
  c1b: ['01', '资料导入', '自动建立知识索引', '解析 → 切片 → 向量化 → 抽取知识卡片，全程在本机完成'],
  c1c: ['01', '资料导入', '自动抽取知识卡片', '事实、规程、陷阱……资料里的要点自动整理成卡片'],
  c2: ['02', '智能问答', '有问必答，先查你的知识库', '接入本地模型，回答只依据你自己的资料'],
  c2b: ['02', '智能问答', '句句有出处', '论断都带引用角标，来源卡片写明出自哪篇、哪一节'],
  c2c: ['02', '智能问答', '检索过程全透明', '检索词、命中的章节、哪一条被引用，一目了然'],
  c3: ['03', '精确溯源', '引用精确到原文的那一句', '点开来源，直接跳到原文，高亮依据的句子'],
  c4: ['04', '本地 · 省 token', '本地模型，省着用 token', '可切换「节省上下文」，每轮输入 token 实时可见'],
  c5: ['05', '纠错学习', '纠正一次，它就记住', '你的纠错会成为待学习的素材'],
  c6: ['06', '自我进化', '一键进化', '蒸馏经验 → 整合去重 → 领域测验 A/B 对照 → 验证有效才生效'],
  c6b: ['06', '自我进化', '没带来提升的经验，不会生效', '测验对照不过关就留作候选；每一轮的分数变化都有记录'],
  c7: ['07', '知识图谱', '懂什么、缺什么，一张图看清', '主题掌握度、知识缺口，还能回放知识的成长过程'],
  c8: ['08', '专家度', '专家度，量化每一次成长', '覆盖率 · 准确率 · 依据度 · 一致性 · 经验密度'],
};

const FONT = `font-family: 'Geist Variable', -apple-system, 'PingFang SC', sans-serif;`;
const SERIF = `font-family: 'Source Han Serif SC', 'Noto Serif SC', 'Songti SC', 'STSong', Georgia, serif; font-weight: 600; letter-spacing: -0.01em;`;
const BG = `
  radial-gradient(1100px 640px at 8% -10%, rgba(167,72,36,.13), transparent 70%),
  radial-gradient(1000px 640px at 104% 112%, rgba(167,72,36,.10), transparent 70%),
  linear-gradient(180deg, #f5f1eb, #efe9e1)`;

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
async function render(name, size, scale, html, transparent = false) {
  await page.setViewportSize(size);
  await page.evaluate(() => {});
  const p = await browser.newPage({ viewport: size, deviceScaleFactor: scale });
  await p.setContent(`<style>*{margin:0;box-sizing:border-box}html,body{width:${size.width}px;height:${size.height}px;overflow:hidden;${transparent ? 'background:transparent' : ''}}</style>${html}`);
  await p.evaluate(() => document.fonts.ready);
  await p.screenshot({ path: `${OUT}/${name}.png`, omitBackground: transparent });
  await p.close();
}
const W = WORLD.win;

// 世界背景（不含窗口阴影）与单独的阴影层（随窗口淡入淡出）
await render('world', { width: WORLD.w, height: WORLD.h }, 2, `<div style="width:100%;height:100%;background:${BG}"></div>`);
await render('shadow', { width: WORLD.w, height: WORLD.h }, 2, `<div style="position:absolute;left:${W.x}px;top:${W.y}px;width:${W.w}px;height:${W.h}px;border-radius:${W.r}px;background:#fbfaf7;box-shadow:0 46px 100px -34px rgba(60,35,20,.38),0 14px 34px -14px rgba(60,35,20,.20),0 0 0 1px rgba(60,35,20,.08)"></div>`, true);
await render('mask', { width: W.w, height: W.h }, 2, `<div style="width:100%;height:100%;background:#000"><div style="width:100%;height:100%;border-radius:${W.r}px;background:#fff"></div></div>`);
// 光标（2x，箭头尖在 (3,2)）
await render('cursor', { width: 30, height: 30 }, 2, `<svg width="30" height="30" viewBox="0 0 24 24" style="filter:drop-shadow(0 1.5px 2px rgba(0,0,0,.3))"><path d="M4 2.5l15 9.2-6.6 1.5 3.9 7.3-2.6 1.4-3.9-7.4L4 19z" fill="#1f1b16" stroke="#fff" stroke-width="1.5" stroke-linejoin="round"/></svg>`, true);

// 字幕：屏幕底部居中的浮动卡片
for (const [id, [num, chapter, title, sub]] of Object.entries(CAPTIONS)) {
  await render(`cap-${id}`, { width: 1920, height: 1080 }, 1, `
    <div style="position:absolute;left:0;right:0;bottom:46px;display:flex;justify-content:center;${FONT}">
      <div style="display:flex;align-items:center;gap:22px;padding:20px 34px 20px 24px;border-radius:22px;background:rgba(253,251,248,.96);
        box-shadow:0 22px 50px -18px rgba(60,35,20,.35),0 0 0 1px rgba(60,35,20,.07)">
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;width:74px;height:74px;border-radius:18px;background:#a74824;color:#fdfcfa">
          <div style="${SERIF}font-size:30px;line-height:1">${num}</div>
        </div>
        <div>
          <div style="display:flex;align-items:baseline;gap:12px">
            <span style="${SERIF}font-size:36px;color:#1c1916">${title}</span>
            <span style="font-size:17px;color:#a74824;letter-spacing:.08em">${chapter}</span>
          </div>
          <div style="margin-top:8px;font-size:21px;color:#6a635e;letter-spacing:.02em">${sub}</div>
        </div>
      </div>
    </div>`, true);
}

// 加速角标
for (const factor of [3, 16, 60]) {
  await render(`speed-${factor}`, { width: 1920, height: 1080 }, 1, `
    <div style="position:absolute;right:48px;top:40px;${FONT}display:flex;align-items:center;gap:8px;padding:9px 16px;border-radius:999px;
      background:rgba(28,25,22,.78);color:#fdfcfa;font-size:17px;letter-spacing:.04em">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="#fdfcfa"><path d="M3 5l8 7-8 7zM13 5l8 7-8 7z"/></svg>×${factor} 加速
    </div>`, true);
}

// 片头：分层渲染，后期逐层错开淡入
const center = (inner) => `<div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;${FONT}">${inner}</div>`;
const ghost = (s) => `<div style="visibility:hidden">${s}</div>`;
const T = {
  logo: `<div style="width:112px;height:112px;filter:drop-shadow(0 20px 34px rgba(167,72,36,.3))">${LOGO.replace('width="32" height="32"', 'width="112" height="112"')}</div>`,
  name: `<div style="margin-top:34px;${SERIF}font-size:108px;line-height:1;color:#1c1916">AgentMem</div>`,
  tag: `<div style="margin-top:26px;${SERIF}font-size:36px;color:#3a332e">会成长为领域专家的本地 AI 知识库</div>`,
  pills: `<div style="margin-top:32px;display:flex;gap:14px">${['本地优先', '引用精确到句', '越用越专业'].map((t) => `<span style="padding:10px 22px;border-radius:999px;background:rgba(255,255,255,.75);border:1px solid #e2dfda;font-size:20px;color:#6a635e">${t}</span>`).join('')}</div>`,
};
const order = ['logo', 'name', 'tag', 'pills'];
for (const key of order) {
  const html = order.map((k) => (k === key ? T[k] : ghost(T[k]))).join('');
  await render(`title-${key}`, { width: 1920, height: 1080 }, 1, center(html), true);
}
await render('title-bg', { width: 1920, height: 1080 }, 1, `<div style="width:100%;height:100%;background:${BG}"></div>`);

const O = {
  head: `<div style="${SERIF}font-size:68px;line-height:1.25;color:#1c1916;text-align:center">用得越久，越懂你的领域</div>`,
  sub: `<div style="margin-top:22px;font-size:26px;color:#6a635e">数据全在本地 · 每个回答都能追溯到原文 · 经验验证有效才生效</div>`,
  brand: `<div style="margin-top:64px;display:flex;align-items:center;gap:16px"><span style="width:58px;height:58px">${LOGO.replace('width="32" height="32"', 'width="58" height="58"')}</span><span style="${SERIF}font-size:42px;color:#1c1916">AgentMem</span></div>`,
};
const oorder = ['head', 'sub', 'brand'];
for (const key of oorder) {
  const html = oorder.map((k) => (k === key ? O[k] : ghost(O[k]))).join('');
  await render(`outro-${key}`, { width: 1920, height: 1080 }, 1, center(html), true);
}

await fs.writeFile(`${OUT}/layout.json`, JSON.stringify(WORLD));
await browser.close();
console.log('assets ok');
