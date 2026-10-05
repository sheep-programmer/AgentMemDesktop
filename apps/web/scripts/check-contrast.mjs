/**
 * 对比度回归：用真实浏览器读**计算样式**，逐元素算 WCAG 对比度。
 *
 * 为什么不是靠眼睛看截图：截图这条路在开发机上不可信（实测改了背景色再截，两张
 * 图字节相同——拿到的是旧帧）。而且「看起来还行」本来就不是判据，4.5:1 才是。
 *
 * 做法：起一个 mock 模式的 dev server（不依赖后端与模型），用无头 Chromium 打开
 * 六个页面 × 深浅两套主题，再逐个打开弹层，把每个含文字的元素的前景色与**逐层
 * 合成后的**背景色算出对比度。低于阈值即失败并列出元素。
 *
 *     node scripts/check-contrast.mjs            # 全量
 *     node scripts/check-contrast.mjs --theme light
 */
import { spawn } from 'node:child_process';
import net from 'node:net';
import process from 'node:process';

import { chromium } from 'playwright';

/** mock 数据里那个示例 Space 的 id。 */
const SPACE_ID = 'space-drug-discovery';

const ROUTES = ['chat', 'library', 'memory', 'evolve', 'expertise', 'settings'];

/**
 * 每个页面上要额外打开的弹层：常驻内容之外，对话框与抽屉同样要扫。
 *
 * `open` 是点击目标的可见文字，`close` 缺省走 Escape。找不到触发点时只记一条
 * “跳过”，不让构建挂掉——mock 数据的形状变了不该表现成对比度失败。
 */
const OVERLAYS = {
  chat: [
    { name: '命令搜索', key: 'ControlOrMeta+k', required: true },
    { name: '上下文策略', button: '选择上下文策略', role: 'menu', itemRole: 'menuitemradio', select: '节省上下文', required: true },
    { name: '对话操作', prepare: '打开全部对话', button: '对话操作：第三代 EGFR-TKI 选择性优化与 Cys797S 耐药策略', role: 'menu', required: true },
  ],
  library: [
    { name: '文档阅读器', open: '激酶选择性突变体筛选与脱靶毒性评估指南.md' },
    { name: '导入资料', button: '导入资料', buttonExact: false, required: true, formTabs: ['粘贴文本', '网页链接'] },
  ],
  memory: [{ name: '知识卡片详情', open: '详情' }],
  expertise: [{
    name: '检索配置对比',
    tab: '测验题',
    open: '检索配置对比',
    required: true,
    run: '开始多臂对比',
    stop: '停止对比',
    stopped: '对比请求已停止',
  }],
};

/** 正文 4.5:1、大字 3:1（WCAG 2.1 AA）。 */
const SCAN = () => {
  const canvas = document.createElement('canvas');
  canvas.width = 2;
  canvas.height = 1;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const cache = new Map();

  // computedStyle 返回的是 oklch(...)，正则解析不了：交给画布还原成 RGBA。
  // 分别在白底与黑底上画一次，两者之差就是 alpha，由此反解出原色。
  const toRGBA = (css) => {
    if (cache.has(css)) return cache.get(css);
    let result = null;
    try {
      const sample = (backdrop) => {
        ctx.clearRect(0, 0, 2, 1);
        ctx.fillStyle = backdrop;
        ctx.fillRect(0, 0, 2, 1);
        ctx.fillStyle = css;
        ctx.fillRect(0, 0, 2, 1);
        const d = ctx.getImageData(0, 0, 1, 1).data;
        return [d[0] / 255, d[1] / 255, d[2] / 255];
      };
      const onWhite = sample('#ffffff');
      const onBlack = sample('#000000');
      const alpha = 1 - (onWhite[0] - onBlack[0]);
      result =
        alpha <= 0.002
          ? { r: 0, g: 0, b: 0, a: 0 }
          : { r: onBlack[0] / alpha, g: onBlack[1] / alpha, b: onBlack[2] / alpha, a: alpha };
    } catch {
      result = null;
    }
    cache.set(css, result);
    return result;
  };

  const lin = (c) => (c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4));
  const lum = (c) => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  const over = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a),
    a: 1,
  });
  const ratio = (a, b) => {
    const l1 = lum(a);
    const l2 = lum(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  };

  // 背景要一层层往上合成：半透明底色叠在半透明底色上是界面里的常态
  const bgOf = (el) => {
    const stack = [];
    let node = el;
    while (node) {
      const c = toRGBA(getComputedStyle(node).backgroundColor);
      if (c && c.a > 0) {
        stack.push(c);
        if (c.a >= 0.999) break;
      }
      node = node.parentElement;
    }
    let acc =
      stack.length && stack[stack.length - 1].a >= 0.999
        ? stack.pop()
        : { r: 1, g: 1, b: 1, a: 1 };
    while (stack.length) acc = over(stack.pop(), acc);
    return acc;
  };

  const out = [];
  document.querySelectorAll('*').forEach((el) => {
    const text = Array.from(el.childNodes)
      .filter((n) => n.nodeType === 3)
      .map((n) => n.textContent.trim())
      .join('')
      .trim();
    if (!text) return;
    const rect = el.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) return;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || Number(s.opacity) === 0) return;
    const fg = toRGBA(s.color);
    if (!fg || fg.a === 0) return;
    const bg = bgOf(el);
    const value = ratio(fg.a < 1 ? over(fg, bg) : fg, bg);
    const size = parseFloat(s.fontSize);
    const large = size >= 24 || (size >= 18.66 && parseInt(s.fontWeight, 10) >= 700);
    const need = large ? 3 : 4.5;
    if (value < need) {
      out.push({
        ratio: Math.round(value * 100) / 100,
        need,
        size,
        text: text.slice(0, 24),
        cls: String(el.className).slice(0, 80),
      });
    }
  });
  return out.sort((a, b) => a.ratio - b.ratio);
};

async function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function waitForServer(url, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // 还没起来
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`dev server 在 ${timeoutMs}ms 内没起来：${url}`);
}

async function scanOnce(page, label, failures) {
  const found = await page.evaluate(SCAN);
  if (found.length) {
    failures.push({ label, items: found });
    for (const item of found.slice(0, 8)) {
      process.stderr.write(`  [${label}] ${item.ratio} < ${item.need} ${JSON.stringify(item.text)} ${item.cls}\n`);
    }
  }
  return found.length;
}

async function main() {
  const only = process.argv.includes('--theme')
    ? process.argv[process.argv.indexOf('--theme') + 1]
    : null;
  const themes = only ? [only] : ['light', 'dark'];

  const port = await freePort();
  // 用 localhost 而不是 127.0.0.1：vite 默认只绑 localhost，这台机器上它先解析到 ::1
  const base = `http://localhost:${port}`;
  const server = spawn('npx', ['vite', '--port', String(port), '--strictPort'], {
    env: { ...process.env, VITE_USE_MOCK: '1' },
    stdio: 'ignore',
  });

  const failures = [];
  const skipped = [];
  let browser;
  try {
    await waitForServer(base);
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

    for (const theme of themes) {
      await page.goto(base);
      await page.evaluate((value) => localStorage.setItem('agentmem_theme', value), theme);

      for (const route of ROUTES) {
        await page.goto(route === 'settings' ? `${base}/settings` : `${base}/s/${SPACE_ID}/${route}`);
        await page.waitForTimeout(1500);
        const n = await scanOnce(page, `${theme} · ${route}`, failures);
        process.stdout.write(`  ${theme} · ${route.padEnd(10)} ${n ? `✗ ${n}` : '✓'}\n`);

        for (const overlay of OVERLAYS[route] ?? []) {
          try {
            if (overlay.prepare) await page.getByRole('button', { name: overlay.prepare, exact: true }).click();
            if (overlay.tab) {
              await page.getByRole('tab', { name: overlay.tab, exact: true }).click();
              const tabFailures = await scanOnce(page, `${theme} · ${route} · ${overlay.tab}`, failures);
              process.stdout.write(`  ${theme} · ${route} · ${overlay.tab} ${tabFailures ? `✗ ${tabFailures}` : '✓'}\n`);
            }
            if (overlay.key) {
              await page.keyboard.press(overlay.key);
            } else if (overlay.button) {
              await page.getByRole('button', { name: overlay.button, exact: overlay.buttonExact !== false }).click();
            } else {
              await page.getByText(overlay.open, { exact: false }).first().click({ timeout: 4000 });
            }
            if (overlay.required) await page.getByRole(overlay.role ?? 'dialog').waitFor({state:'visible'});
            await page.waitForTimeout(1800);
            if (!(await page.getByRole(overlay.role ?? 'dialog').count())) {
              if (overlay.required) throw new Error(`${overlay.name}没有打开`);
              skipped.push(`${theme} · ${route} · ${overlay.name}（没打开）`);
            } else {
              if (overlay.name === '命令搜索') {
                const selectedOnly = await page.evaluate(() => {
                  const selected = document.querySelector('[cmdk-item][data-selected="true"]');
                  const other = document.querySelector('[cmdk-item][data-selected="false"]');
                  return selected && other && getComputedStyle(selected).backgroundColor !== getComputedStyle(other).backgroundColor;
                });
                if (!selectedOnly) throw new Error('命令列表选中项与未选中项的背景没有区分');
              }
              const m = await scanOnce(page, `${theme} · ${route} · ${overlay.name}`, failures);
              process.stdout.write(`  ${theme} · ${route} · ${overlay.name} ${m ? `✗ ${m}` : '✓'}\n`);
              for (const tab of overlay.formTabs ?? []) {
                await page.getByRole('tab', {name:tab,exact:true}).click();
                const form = await scanOnce(page, `${theme} · ${overlay.name} · ${tab}`, failures);
                process.stdout.write(`  ${theme} · ${overlay.name} · ${tab} ${form ? `✗ ${form}` : '✓'}\n`);
                if (tab === '网页链接') {
                  await page.getByRole('textbox', {name:'网页地址',exact:true}).fill('example.com/missing-protocol');
                  await page.getByRole('button', {name:'导入网页',exact:true}).click();
                  await page.getByText('请输入完整的网页地址', {exact:false}).waitFor({state:'visible'});
                  const invalid = await scanOnce(page, `${theme} · 导入网页 · 输入校验`, failures);
                  process.stdout.write(`  ${theme} · 导入网页 · 输入校验 ${invalid ? `✗ ${invalid}` : '✓'}\n`);
                }
              }
              if (overlay.select) {
                await page.getByRole(overlay.itemRole ?? 'menuitem', { name: new RegExp(overlay.select) }).click();
                const trigger = await page.getByRole('button', { name: overlay.button, exact: true }).textContent();
                if (!trigger.includes('节省')) throw new Error('节省上下文选项没有生效');
                const chosen = await scanOnce(page, `${theme} · chat · 节省模式`, failures);
                process.stdout.write(`  ${theme} · chat · 节省模式 ${chosen ? `✗ ${chosen}` : '✓'}\n`);
              }
              if (overlay.run) {
                await page.getByRole('button', { name: new RegExp(overlay.run) }).click();
                const stop = page.getByRole('button', { name: overlay.stop, exact: true });
                await stop.waitFor({ state: 'visible' });
                const active = await scanOnce(page, `${theme} · ${overlay.name} · 运行中`, failures);
                process.stdout.write(`  ${theme} · ${overlay.name} · 运行中 ${active ? `✗ ${active}` : '✓'}\n`);
                await stop.click();
                await page.getByText(overlay.stopped, { exact: false }).waitFor({ state: 'visible' });
                const stopped = await scanOnce(page, `${theme} · ${overlay.name} · 已停止`, failures);
                process.stdout.write(`  ${theme} · ${overlay.name} · 已停止 ${stopped ? `✗ ${stopped}` : '✓'}\n`);
              }
            }
          } catch (error) {
            if (overlay.required) throw error;
            skipped.push(`${theme} · ${route} · ${overlay.name}（找不到入口）`);
          } finally {
            await page.keyboard.press('Escape');
            await page.waitForTimeout(400);
          }
        }
        if (route === 'settings') {
          for (const tab of ['外观与主题', '数据与存储', '关于系统']) {
            await page.getByRole('tab', {name:tab,exact:true}).click();
            await page.waitForTimeout(500);
            const count=await scanOnce(page, `${theme} · settings · ${tab}`, failures);
            process.stdout.write(`  ${theme} · settings · ${tab} ${count ? `✗ ${count}` : '✓'}\n`);
          }
        }
      }
    }
  } finally {
    if (browser) await browser.close();
    server.kill();
  }

  if (skipped.length) {
    process.stdout.write(`\n跳过的弹层（入口没找到，不算失败）：\n`);
    for (const item of skipped) process.stdout.write(`  - ${item}\n`);
  }

  if (failures.length) {
    process.stderr.write('\n对比度不达标：\n');
    for (const { label, items } of failures) {
      process.stderr.write(`\n[${label}]\n`);
      for (const item of items.slice(0, 12)) {
        process.stderr.write(
          `  ${item.ratio} < ${item.need}  ${item.size}px  ${JSON.stringify(item.text)}  ${item.cls}\n`,
        );
      }
      if (items.length > 12) process.stderr.write(`  …还有 ${items.length - 12} 处\n`);
    }
    process.exit(1);
  }

  process.stdout.write('\n全部达标（正文 4.5:1 / 大字 3:1）\n');
}

main().catch((error) => {
  process.stderr.write(`${error?.stack || error}\n`);
  process.exit(1);
});
