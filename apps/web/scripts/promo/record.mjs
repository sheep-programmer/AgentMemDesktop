// 宣传片 v3 录制：一镜到底，核心流程完整走一遍。
// 画面：CDP screencast 2x 帧；鼠标轨迹、点击、镜头焦点、字幕、加速段全部写进时间线，
// compose.py 按 60fps 渲染（镜头 / 光标缓动、静止段自动压缩、配乐与音效都在后期）。
import { chromium } from 'playwright';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const DIR = process.env.PROMO_DIR ?? '/tmp/agentmem-promo';
const FRAMES = path.join(DIR, 'frames');
const BASE = 'http://localhost:5173';
const API = 'http://127.0.0.1:8765/api/v1';
const SPACE = process.env.SPACE;
const UPLOAD = fileURLToPath(new URL('./demo/PCS储能变流器运维手册.md', import.meta.url));
const VIEW = { width: 1440, height: 810 };
const QUESTION = '电芯报 E07 之后多久能自动恢复？一天触发三次会怎样？';
const now = () => Date.now() / 1000;

// ---- 每一遍都从同一个状态开始 ----
{
  const docs = await (await fetch(`${API}/spaces/${SPACE}/documents?limit=100`)).json();
  for (const doc of docs.items) {
    if (doc.title.startsWith('PCS储能变流器运维手册')) {
      await fetch(`${API}/spaces/${SPACE}/documents/${doc.id}`, { method: 'DELETE' });
    }
  }
  const convs = await (await fetch(`${API}/spaces/${SPACE}/conversations?limit=100`)).json();
  for (const conv of convs.items) {
    if (conv.title.startsWith('电芯报 E07')) await fetch(`${API}/conversations/${conv.id}`, { method: 'DELETE' });
  }
}

const browser = await chromium.launch({ headless: true, args: ['--force-device-scale-factor=2'] });
const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2, locale: 'zh-CN' });
const page = await context.newPage();
const cdp = await context.newCDPSession(page);

const timeline = { frames: [], events: [], view: VIEW };
let recording = false;
await fs.rm(FRAMES, { recursive: true, force: true });
await fs.mkdir(FRAMES, { recursive: true });
let pending = Promise.resolve();
cdp.on('Page.screencastFrame', ({ data, metadata, sessionId }) => {
  if (recording) {
    const file = path.join(FRAMES, `${String(timeline.frames.length).padStart(6, '0')}.jpg`);
    timeline.frames.push({ file, t: metadata.timestamp });
    pending = pending.then(() => fs.writeFile(file, Buffer.from(data, 'base64')));
  }
  cdp.send('Page.screencastFrameAck', { sessionId }).catch(() => {});
});

const log = (event) => timeline.events.push({ t: now(), ...event });
const wait = (ms) => page.waitForTimeout(ms);
let mouse = { x: 720, y: 460 };

const ease = (u) => (u < 0.5 ? 4 * u * u * u : 1 - (-2 * u + 2) ** 3 / 2);
async function glide(x, y, ms = 650) {
  const from = { ...mouse };
  log({ type: 'move', from: [from.x, from.y], to: [x, y], dur: ms / 1000 });
  const steps = Math.max(8, Math.round(ms / 20));
  const start = Date.now();
  for (let i = 1; i <= steps; i += 1) {
    const u = ease(i / steps);
    await page.mouse.move(from.x + (x - from.x) * u, from.y + (y - from.y) * u);
    const lag = start + (ms * i) / steps - Date.now();
    if (lag > 0) await wait(lag);
  }
  mouse = { x, y };
}
async function boxOf(locator) {
  await locator.waitFor({ state: 'visible', timeout: 30_000 });
  return locator.boundingBox();
}
async function clickOn(locator, ms = 650) {
  const box = await boxOf(locator);
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await glide(x, y, ms);
  await wait(100);
  log({ type: 'click', at: [x, y] });
  await page.mouse.down();
  await wait(60);
  await page.mouse.up();
}
/** 镜头对准某块区域（CSS 像素）；null 为全景。 */
async function focus(target, { pad = 1.3, dur = 0.85, minWidth = 760 } = {}) {
  if (target === null) return log({ type: 'camera', rect: null, dur });
  const box = typeof target.boundingBox === 'function' ? await boxOf(target) : target;
  log({ type: 'camera', rect: [box.x, box.y, box.width, box.height], pad, dur, minWidth });
}
const union = (...boxes) => {
  const x = Math.min(...boxes.map((b) => b.x));
  const y = Math.min(...boxes.map((b) => b.y));
  return {
    x, y,
    width: Math.max(...boxes.map((b) => b.x + b.width)) - x,
    height: Math.max(...boxes.map((b) => b.y + b.height)) - y,
  };
};
const caption = (id) => log({ type: 'caption', id });
const speed = (factor) => log({ type: 'speed', factor });
const nav = (label) => page.getByRole('navigation', { name: '主要功能' }).getByText(label, { exact: true }).first();
/** 页面里某段文字所在、宽度超过 minWidth 的最近祖先的位置 */
const blockOf = (text, minWidth = 200) =>
  page.evaluate(([needle, min]) => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.data.includes(needle)) continue;
      let el = node.parentElement;
      while (el && el.getBoundingClientRect().width < min) el = el.parentElement;
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0) continue;
      return { x: r.x, y: r.y, width: r.width, height: r.height };
    }
    return null;
  }, [text, minWidth]);

// ---- 预热 ----
await page.goto(`${BASE}/s/${SPACE}/library`);
for (const route of ['chat', 'graph', 'expertise', 'evolve', 'memory', 'library']) {
  await page.goto(`${BASE}/s/${SPACE}/${route}`);
  await wait(1200);
}
await page.mouse.move(mouse.x, mouse.y);
await wait(600);

await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 90, maxWidth: 2880, maxHeight: 1620, everyNthFrame: 1 });
recording = true;
timeline.start = now();
await wait(400);

// ============ 01 资料导入 ============
caption('c1');
await wait(900);
await clickOn(page.getByRole('button', { name: /导入资料/ }).first(), 800);
await page.getByRole('dialog').waitFor();
await wait(250);
await focus(page.getByRole('dialog'), { pad: 1.2 });
await wait(900);
const chooserPromise = page.waitForEvent('filechooser');
await clickOn(page.getByRole('button', { name: '选择文件' }), 700);
const chooser = await chooserPromise;
await wait(200);
await chooser.setFiles(UPLOAD);
await page.getByRole('dialog').waitFor({ state: 'hidden', timeout: 30_000 });
caption('c1b');
await page.getByLabel(/^选择资料：PCS储能变流器运维手册/).waitFor({ timeout: 30_000 });
await wait(250);
const ingest = await page.evaluate(() => {
  const check = document.querySelector('[aria-label^="选择资料：PCS储能变流器运维手册"]');
  let card = check;
  while (card && card.getBoundingClientRect().width < 220) card = card.parentElement;
  const bar = [...document.querySelectorAll('span')].find((el) => el.textContent?.startsWith('正在处理'))?.closest('.rounded-xl');
  const a = card.getBoundingClientRect();
  const b = bar ? bar.getBoundingClientRect() : a;
  return { card: { x: a.x, y: a.y, width: a.width, height: a.height }, bar: { x: b.x, y: b.y, width: Math.min(b.width, 620), height: b.height } };
});
await focus(union(ingest.card, ingest.bar), { pad: 1.12 });
await glide(ingest.card.x + ingest.card.width - 40, ingest.card.y + ingest.card.height - 30, 700);
speed(10);
await page.waitForFunction(() => {
  let card = document.querySelector('[aria-label^="选择资料：PCS储能变流器运维手册"]');
  while (card && card.getBoundingClientRect().width < 220) card = card.parentElement;
  return !!card?.querySelector('[aria-label="资料已就绪"]');
}, undefined, { timeout: 300_000, polling: 400 });
speed(1);
await wait(900);

// 抽取出来的知识卡片
caption('c1c');
await focus(null);
await clickOn(nav('知识记忆'), 800);
await page.locator('div.border-l-4').first().waitFor();
await wait(500);
{
  const cards = page.locator('div.border-l-4');
  const first = await boxOf(cards.nth(0));
  const third = await boxOf(cards.nth(2));
  await focus(union(first, third), { pad: 1.1 });
  await glide(first.x + first.width / 2, first.y + first.height / 2, 700);
}
await wait(2000);
await focus(null);

// ============ 02 问答 ============
caption('c2');
await clickOn(nav('知识对话'), 800);
await wait(600);
await clickOn(page.getByRole('button', { name: '新对话' }).first(), 650);
await wait(500);
const input = page.getByRole('textbox', { name: '向知识库提问' });
{
  const t = await boxOf(input);
  await focus({ x: t.x - 20, y: t.y - 30, width: t.width + 40, height: t.height + 90 }, { pad: 1.15, minWidth: 960 });
}
await clickOn(input, 650);
await page.keyboard.type(QUESTION, { delay: 55 });
await wait(300);
await clickOn(page.getByRole('button', { name: '发送问题' }), 500);
speed(3);
await page.waitForFunction(() => (document.querySelector('.answer-prose')?.textContent ?? '').replace('思考中...', '').trim().length > 4, undefined, { timeout: 120_000 });
speed(1);
await focus({ x: 300, y: 150, width: 790, height: 420 }, { pad: 1.08 });
await glide(1010, 600, 700);
await page.waitForFunction(
  () => !document.querySelector('[aria-label="停止生成"]') && document.body.innerText.includes('来源 ·'),
  undefined,
  { timeout: 120_000 },
);
await wait(500);
caption('c2b');
{
  const answer = await boxOf(page.locator('.answer-prose').last());
  const sources = await boxOf(page.locator('ul.grid').last());
  await focus(union(answer, sources), { pad: 1.1 });
  await glide(sources.x + 60, sources.y + 30, 700);
}
await wait(1800);
caption('c2c');
const retrievalButton = page.locator('.retrieval-block button[aria-controls]').last();
await clickOn(retrievalButton, 700);
await wait(450);
await focus(page.locator('.retrieval-block').last(), { pad: 1.12 });
await wait(2200);
await clickOn(retrievalButton, 500);
await wait(400);

// ============ 03 引用定位到原句 ============
caption('c3');
const sourceCard = page.locator('ul.grid').last().locator('li').first().locator('button').first();
await clickOn(sourceCard, 700);
await page.getByText('依据原句').first().waitFor();
await wait(300);
{
  const pop = await page.locator('[data-slot="popover-content"]').last().boundingBox();
  if (pop) await focus(pop, { pad: 1.25 });
}
await wait(1800);
await clickOn(page.locator('[data-slot="popover-content"]').last().getByRole('button', { name: /原文/ }), 650);
await page.waitForFunction(() => CSS.highlights && CSS.highlights.has('agentmem-quote'), undefined, { timeout: 20_000 });
await wait(1600);
{
  const para = await page.evaluate(() => {
    const block = document.querySelector('[data-chunk-highlight]');
    const r = block.getBoundingClientRect();
    const range = [...CSS.highlights.get('agentmem-quote')][0].getBoundingClientRect();
    return { block: { x: r.x, y: r.y, width: r.width, height: r.height }, quote: { x: range.x, y: range.y, width: range.width, height: range.height } };
  });
  await focus({ x: para.block.x, y: para.block.y - 50, width: para.block.width, height: para.block.height + 60 }, { pad: 1.08 });
  await glide(para.quote.x + para.quote.width - 30, para.quote.y + para.quote.height + 14, 700);
}
await wait(2400);
await focus(null);
await page.keyboard.press('Escape');
await wait(700);

// ============ 04 本地模型 · 省 token ============
caption('c4');
{
  const t = await boxOf(input);
  const usage = await blockOf('本轮输入估算', 400);
  const area = usage ? union(usage, { x: t.x - 16, y: t.y, width: t.width + 32, height: t.height + 64 }) : t;
  await focus(area, { pad: 1.1, minWidth: 960 });
}
await clickOn(page.getByRole('button', { name: '选择上下文策略' }), 750);
await page.getByRole('menu').waitFor();
await wait(250);
await focus(page.getByRole('menu'), { pad: 1.6, minWidth: 960 });
await glide(mouse.x + 40, mouse.y - 90, 600);
await wait(1700);
await page.keyboard.press('Escape');
await wait(300);
{
  const chip = await boxOf(page.getByRole('button', { name: '选择对话模型' }));
  await focus({ x: chip.x - 420, y: chip.y - 90, width: chip.width + 440, height: 140 }, { pad: 1.2, minWidth: 960 });
  await glide(chip.x + chip.width / 2, chip.y + chip.height / 2, 700);
}
await wait(1500);
await focus(null);

// ============ 05 纠错 ============
caption('c5');
const fix = page.getByRole('button', { name: '纠错' }).last();
await clickOn(fix, 750);
const fixBox = page.getByRole('textbox', { name: '正确的事实或做法' });
await fixBox.waitFor();
{
  const pop = await page.locator('[data-slot="popover-content"]').last().boundingBox();
  await focus(pop, { pad: 1.35 });
}
await clickOn(fixBox, 450);
await page.keyboard.type('E07 锁定后复位前，必须先检查液冷机组并确认电芯温度已低于 45℃，否则复位后会再次触发。', { delay: 38 });
await wait(300);
await clickOn(page.getByRole('button', { name: '保存纠错' }), 600);
await wait(1100);
await focus(null);

// ============ 06 进化 ============
caption('c6');
await clickOn(nav('进化中心'), 800);
await wait(900);
const start = page.getByRole('button', { name: '开始一次进化' });
await clickOn(start, 750);
const confirm = page.getByRole('alertdialog').or(page.getByRole('dialog')).first();
await confirm.waitFor();
await wait(250);
await focus(confirm, { pad: 1.3 });
await wait(1900);
await clickOn(confirm.getByRole('button', { name: '开始进化' }), 650);
await wait(900);
{
  const stages = await blockOf('阶段 1', 600);
  const hero = await blockOf('待学习素材与闭环进化', 600);
  await focus(stages ? { x: stages.x, y: stages.y - 20, width: stages.width, height: 420 } : hero, { pad: 1.08 });
}
await glide(1200, 650, 700);
speed(30);
await page.getByText('本次闭环进化圆满完成').waitFor({ timeout: 900_000 });
speed(1);
caption('c6b');
{
  const done = page.getByText('本次闭环进化圆满完成').locator('xpath=ancestor::div[contains(@class,"rounded")][1]');
  await done.scrollIntoViewIfNeeded();
  await wait(500);
  await focus(done, { pad: 1.15 });
  const b = await done.boundingBox();
  await glide(b.x + b.width / 2 + 120, b.y + b.height - 40, 700);
}
await wait(2600);
await focus(null);

// ============ 07 知识图谱 ============
caption('c7');
await clickOn(nav('知识图谱'), 800);
await wait(1600);
await clickOn(page.getByRole('button', { name: /回放成长过程/ }), 750);
{
  const graph = await boxOf(page.getByLabel('知识网络图'));
  await focus(graph, { pad: 1.02, dur: 1.4 });
}
await wait(6200);
{
  const panel = await boxOf(page.getByLabel('掌握程度与成长脉络'));
  await focus({ x: panel.x, y: panel.y, width: panel.width, height: Math.min(panel.height, 520) }, { pad: 1.15, dur: 1.0 });
  await glide(panel.x + panel.width / 2, panel.y + 200, 700);
}
await wait(2200);
await focus(null);

// ============ 08 专家度 ============
caption('c8');
await clickOn(nav('专家评测'), 800);
await wait(1600);
await page.getByText('五维雷达基准对比').first().waitFor({ timeout: 30_000 });
await page.getByText('综合专家度').first().waitFor({ timeout: 30_000 });
await wait(400);
{
  const radar = await blockOf('五维雷达基准对比', 400);
  const score = await blockOf('综合专家度', 400);
  await focus(union(radar, score), { pad: 1.06 });
  await glide(score.x + 160, score.y + 140, 700);
}
await wait(2600);
await focus(null, { dur: 1.0 });
await wait(900);

caption(null);
await wait(300);
recording = false;
await cdp.send('Page.stopScreencast');
timeline.end = now();
await pending;
await fs.writeFile(path.join(DIR, 'timeline.json'), JSON.stringify(timeline));
console.log('frames', timeline.frames.length, 'seconds', (timeline.end - timeline.start).toFixed(1), 'events', timeline.events.length);
await browser.close();
