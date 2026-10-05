# AgentMem · 前端设计与实现规范

> 目标质感：Linear 的精密 + Raycast 的键盘流 + Obsidian 的知识掌控感。
> 反面教材：任何 Ant Design Pro / 通用后台模板的既视感。

---

## 0. 铁律：界面上的每个数字都要能指到它的后端来源

这条排在技术栈前面，因为它被违反过很多次，而且每次都很贵。

规则

1. 不准编数。 界面上任何数字、比例、耗时、体积、条数，都必须来自 API 响应或由其直接计算。
   真值取不到就显示 `—` / 「取不到」/ 不显示这一项，绝不写一个看着合理的默认值。
   `?? 0.85`、`: '0.8s'`、`'124.6 MB'` 这类兜底等同于伪造测量值。
   （可编辑表单的初始值不在此列——那是输入默认值，不是测量结果。）
2. 不准假装做过事。 按钮如果不发请求，就不要弹「已记录」「已导出」「已清空」。
   没有后端能力就撤掉这个按钮，或者明确标注为未实现。
3. 演示数据只许活在 `isMockMode()` 分支里，且分支必须在函数入口处早返回，
   不能和真实路径交织。
4. SSE / 流式事件的 payload 不许丢。 后端推了 detail 就要用；
   只取事件名去点灯、正文另外写死，是本项目出现过的最严重的一类 bug。

为什么

本产品的卖点是「可验证的进化」「每个回答都能下钻到原文」。一旦界面上出现编造的数字，
用户失去的不只是这一个数——是对全部数字的信任，包括那些真的。
而且这类 bug 不会崩、不报错、测试也照过，只能靠人眼比对，代价极高。

实际踩过的（均已修复，详见 `05-ROADMAP.md` 技术债表）

- 进化阶段面板四个阶段的 summary 与 metrics 全是写死常量（「从 17 条反馈蒸馏出 7 条」「基线 62.4 → 71.8」），
  而后端 SSE 每阶段都推了真实 detail，前端收到后直接丢弃。
- 存储面板的 SQLite「18.4 MB」/ LanceDB「124.6 MB」是假的，路径也是错的；
  三个按钮各弹一句「已导出」「已清空，释放 320 MB」「请选择备份文件」，都不发请求。
- 输入框的模型选择器三个选项写死，`selectedModel` 从不发给后端（后端只认 `llm_role`），
  实际运行的服务与界面预置名称不一致。
- 证据栏的经验 👍/👎 弹「已记录正向反馈」，但后端根本没有单条经验的反馈端点。
- 裁判解析失败按 0 分计入总分（后端侧的同类问题：把「没测到」当成「答得差」）。

---

## 1. 技术栈（已校验，v1 冻结）

| 项 | 选型 | 关键注意 |
|---|---|---|
| 框架 | React 19 + Vite + TypeScript | 纯 SPA，为 Tauri 打包留路 |
| 样式 | Tailwind CSS v4 | ⚠️ v4 是 CSS-First：没有 `tailwind.config.js`，主题写在 CSS 里用 `@theme`；用 `@import "tailwindcss"` 取代旧的三条 `@tailwind` 指令；配 `@tailwindcss/vite` 插件，不需要 `postcss.config.js` |
| 组件 | shadcn/ui | `npx shadcn@latest init`，已原生支持 Tailwind v4 + React 19，组件不再有 `forwardRef` 包装 |
| 服务端状态 | TanStack Query v5 | 所有 REST 请求 |
| 客户端状态 | Zustand | UI 折叠、当前 space、命令面板开关 |
| 动画 | motion v12+ | ⚠️ 包名是 `motion`（原 framer-motion），导入 `from "motion/react"` |
| 流式 Markdown | streamdown | 专为 LLM 流式设计，自动补全未闭合的 ``` 与标记，避免打字机抖动；内置 Shiki + KaTeX |
| 长列表 | react-virtuoso | 消息高度不定，优于需预估高度的方案 |
| 命令面板 | cmdk | shadcn `Command` 底层 |
| 文件拖拽 | react-dropzone | |
| 知识图谱 | react-force-graph-2d | Obsidian 式呼吸感力导向；节点上万时退化到 Cytoscape.js |
| 图表 | Recharts | 雷达图（专家度）、折线图（成长曲线） |
| 路由 | React Router v7（data router） | |
| 图标 | lucide-react | |

React 19 依赖冲突处理：用 pnpm；必要时 `package.json` 加
```json
"overrides": { "react": "^19.0.0", "react-dom": "^19.0.0" }
```

---

## 2. 设计系统

### 2.1 色彩（OKLCH，写在 `src/styles/theme.css`）

原则
- 深色模式禁止纯黑：用低饱和深冷灰，长时间阅读不刺眼
- 浅色模式用温润纸感灰白，不用纯白
- 主色克制：大面积中性色 + 小面积强调色。强调色用于「AI 正在思考 / 检索命中 / 经验生效」这类语义状态
- 层次靠微妙边框（`1px`，白 8%–10% 或黑 5%）+ 多层细投影，而非大色块填充

```css
@import "tailwindcss";

@layer base {
  :root {
    --background:        oklch(0.985 0.003 250);
    --foreground:        oklch(0.185 0.012 255);
    --card:              oklch(1    0     0);
    --muted:             oklch(0.955 0.005 250);
    --muted-foreground:  oklch(0.52  0.015 255);
    --border:            oklch(0.90  0.006 255);
    --primary:           oklch(0.55  0.19  265);   /* 靛蓝 */
    --primary-foreground:oklch(0.99  0     0);
    --accent-ai:         oklch(0.70  0.14  200);   /* 青 —— AI 状态 */
    --accent-insight:    oklch(0.68  0.16  150);   /* 绿 —— 经验生效 */
    --accent-warn:       oklch(0.72  0.16  70);
    --destructive:       oklch(0.58  0.21  27);
    --radius:            0.625rem;
  }

  .dark {
    --background:        oklch(0.145 0.010 255);
    --foreground:        oklch(0.945 0.005 250);
    --card:              oklch(0.185 0.012 255);
    --muted:             oklch(0.235 0.012 255);
    --muted-foreground:  oklch(0.65  0.015 255);
    --border:            oklch(0.98  0.005 255 / 10%);
    --primary:           oklch(0.66  0.17  265);
    --accent-ai:         oklch(0.75  0.15  200);
    --accent-insight:    oklch(0.74  0.16  150);
  }
}

@theme inline {
  --color-background: var(--background);
  --color-foreground: var(--foreground);
  --color-card:       var(--card);
  --color-muted:      var(--muted);
  --color-border:     var(--border);
  --color-primary:    var(--primary);
  --color-accent-ai:  var(--accent-ai);
  --color-accent-insight: var(--accent-insight);
  --radius-lg: var(--radius);
  --font-sans: "Inter", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  --font-mono: "JetBrains Mono", "SF Mono", ui-monospace, monospace;
}
```

### 2.2 排印与尺度

| 项 | 规定 |
|---|---|
| 正文 | 14px / `leading-relaxed`；对话正文 15px |
| 标题层级 | 仅 3 级：20px semibold / 16px medium / 13px medium uppercase tracking-wide muted |
| 代码 | 13px mono |
| 间距 | 4 的倍数；卡片内 padding 16px，区块间 24px |
| 圆角 | 10px（卡片）/ 8px（按钮、输入）/ 6px（标签） |
| 动效时长 | 120ms（hover）/ 200ms（展开）/ 320ms（页面转场）；缓动 `cubic-bezier(0.32, 0.72, 0, 1)` |
| 信息密度 | 偏高。侧栏行高 32px，列表行高 40px |

### 2.3 必须遵守的观感细节

- 所有可交互元素有明确 hover / focus-visible 态，focus ring 用 primary 2px
- 加载态用骨架屏，不用转圈 spinner（除按钮内联）
- 空状态必须有插画感的图标 + 一句引导文案 + 一个主行动按钮，禁止只写「暂无数据」
- 流式输出时光标闪烁块 + 内容淡入，不要整段跳变
- 深浅色切换跟随系统，且可手动覆盖，切换有 200ms 过渡

---

## 3. 布局骨架

```
┌────┬──────────────────────────────────────────────────────┐
│    │  TopBar: 当前 Space 名 · 专家度徽章 · ⌘K · 主题 · 设置 │
│ 图 ├──────────────────────────────────────────────────────┤
│ 标 │                                                      │
│ 导 │                   主内容区                            │
│ 航 │                                                      │
│ 栏 │                                                      │
│ 56 │                                                      │
│ px │                                                      │
└────┴──────────────────────────────────────────────────────┘
```

左侧主导航（56px 图标栏，hover 展开为 220px）
| 图标 | 页面 | 路由 |
|---|---|---|
| 💬 | 对话 Chat | `/s/:spaceId/chat` |
| 📚 | 知识库 Library | `/s/:spaceId/library` |
| 🧠 | 记忆 Memory | `/s/:spaceId/memory` |
| 🌱 | 进化 Evolve | `/s/:spaceId/evolve` |
| 📊 | 专家度 Expertise | `/s/:spaceId/expertise` |
| ⚙️ | 设置 Settings | `/settings` |

顶部是 Space 切换器（点击弹出 cmdk 面板，显示各 Space 的图标/名称/专家度环形进度）。

---

## 4. 六大页面详规

### 4.1 Chat —— 对话

三栏：`会话列表(260px) | 对话主区(flex) | 证据侧栏(360px，可收起)`

对话主区
- 用户消息：右对齐气泡，浅色底
- AI 消息：左对齐无气泡，纯文本流 + 左侧 2px 强调竖线
- 回答上方的「过程条」（核心亮点）——流式期间实时更新，完成后折叠成一行摘要：
  ```
  ⟳ 改写查询 → 🔍 检索到 8 篇资料 → 🧩 应用 3 条经验 → ✍️ 生成中
  ```
  点击任一环节展开对应详情；完成后显示 `deepseek-chat · 3.0k→428 tok · 4.2s`
- 正文用 `streamdown` 渲染。引用标记 `[^c3]` 渲染成可点击的上标芯片，hover 弹出原文片段浮层，点击在右侧证据栏定位并高亮
- 消息底部操作条：复制 / 重新生成 / 👍 / 👎 / ✏️ 纠正
  - 点 👎 弹出输入框问「哪里不对？」→ 写入 `feedback(kind=down, comment)`
  - 点 ✏️ 纠正 → 用户直接改写答案 → 写入 `feedback(kind=correction)` → 提示「已记录，下次进化时会学习」
- 输入框：多行自适应，`⌘Enter` 发送。左侧开关芯片：`检索 ▾`（全部/指定文档）、`经验 ✓`、`模型 ▾`

证据侧栏
- Tab 1「资料」：命中的 chunk 卡片，显示文档名、页码、相似度条、片段。点击打开文档阅读器并高亮
- Tab 2「经验」：本次注入的 Insight，显示 trigger / guidance / 置信度环。每条可直接 👍👎 反馈其有用性
- Tab 3「轨迹」：原始 trace JSON（折叠树），给高级用户看

### 4.2 Library —— 知识库

- 顶部：拖拽上传区（`react-dropzone`，整页可拖入）+ 「粘贴文本」「导入网址」按钮
- 视图切换：卡片网格 / 表格
- 每个文档卡片：类型图标、标题、大小、chunk 数、状态进度环（parsing→embedding→ready 实时走 SSE）
- 处理中文档在顶部形成一条「摄取队列」横条，可展开看逐个进度
- 点击文档 → 阅读器：左侧 Markdown 正文（支持页码锚点高亮）、右侧该文档抽取出的知识卡片列表
- 支持批量选择、批量删除、批量重新处理
- 空状态：「投喂第一份资料，开始培养你的专家」+ 大拖拽区

### 4.3 Memory —— 记忆

Tab 三分：

① 知识卡片 L2
- 瀑布流卡片，按 kind 用不同左边框色（concept 蓝 / fact 灰 / procedure 紫 / pitfall 橙 / tool 绿）
- 每卡显示置信度小环、来源文档数；点击侧滑抽屉可就地编辑（编辑后标 `已人工校订` 徽章）
- 顶部筛选：kind、置信度区间、搜索

② 知识图谱
- `react-force-graph-2d` 全屏画布，深色背景 + 发光节点
- 节点大小 = mention_count，颜色 = type；边粗细 = weight
- 点击节点 → 右侧面板显示该实体的卡片与相关 chunk
- 搜索框可聚焦到某节点并展开 2 度邻居
- 性能保护：默认只渲染 top 300 节点，可调

③ 经验 L3（最能体现"在学习"的一屏）
- 列表，每条卡片：
  ```
  ┌──────────────────────────────────────────────────┐
  │ [场景] 用户问某加固 APK 的脱壳方法时            │
  │ [做法] 先用 frida-dexdump 尝试内存 dump，再…     │
  │ [依据] 来自 2026-03-12 的一次用户纠正            │
  │ ⬤ 0.85  应用 12 次 / 有效 10 次   ●active       │
  │                    [提升] [归档] [编辑] [溯源]    │
  └──────────────────────────────────────────────────┘
  ```
- 状态用色：`candidate` 灰虚线边框、`active` 绿实线、`conflicted` 橙脉冲动画、`archived` 半透明
- 冲突区置顶：两条矛盾经验并排显示，一键裁决「留 A / 留 B / 合并」
- 排序：置信度 / 最近 / 应用次数

### 4.4 Evolve —— 进化（产品高光页）

- 顶部大卡：「待学习素材 17 条反馈 · 5 条纠错」+ 巨大主按钮 「开始一次进化」
- 点击后进入阶段流水线动画（竖向时间线，每阶段一个卡片，跑完点亮）：
  ```
  ① 蒸馏     从 17 条反馈中提炼出 7 条候选经验   ▸展开看每条
  ② 整合     合并 2 条重复，发现 1 组冲突        ▸去裁决
  ③ 评测     基线 62.4  →  注入后 71.8  (+9.4)   ▸看逐题对比
  ④ 晋升     5 条转为生效，2 条降级归档          ▸展开
  ```
  用 `motion` 做逐阶段淡入 + 进度线生长；数字用滚动计数动画
- 结尾大字展示：专家度 58.2 → 64.7 (+6.5)，配一个简短总结句
- 下方「进化历史」时间线：每次进化的日期、新增/淘汰经验数、分数变化，可展开回看

### 4.5 Expertise —— 专家度

- 左：五维雷达图（Recharts），当前值 vs 上次快照叠加对比
- 右上：总分大数字 + 环形进度 + 与上次的 delta 箭头
- 右下：成长曲线（面积折线，多条线可切维度）
- 下方「知识盲区」：领域大纲树形展开，无资料覆盖的节点标红并提供「去补充资料」按钮
- 「测验集」子 Tab：题目列表、一键自动出题、运行评测（SSE 逐题推送，实时打勾/打叉）

### 4.6 Settings —— 设置

① 模型（最重要，必须做得清爽）
- 上半：角色绑定卡片组，6 个角色（chat / fast / distill / judge / embedding / rerank）各一个下拉，旁边显示当前绑定 provider 的延迟绿点
- 下半：Provider 列表，每行显示 kind 徽章、adapter、base_url、model、状态点、「测试」按钮
- 「+ 添加 Provider」：表单按 adapter 动态切换字段；有 「扫描本机 Ollama / LM Studio」 一键发现按钮，发现后可勾选批量导入
- 切换 embedding 且维度不同时：弹出明确警告「将影响 N 个 Space 的 M 个文档，需要重建索引」，二次确认
- API Key 输入框默认掩码，不回显明文

② 外观：主题、强调色、字号、信息密度（紧凑/舒适）
③ 数据：存储位置、磁盘占用明细、导入/导出、清理缓存
④ 关于：版本、健康检查

---

## 5. 全局交互

### 命令面板 `⌘K`
分组：跳转 Space / 新建对话 / 搜索知识库（实时调 `/search`，结果直接可跳）/ 上传文件 / 触发进化 / 切换主题 / 切换模型角色。

### 快捷键
| 键 | 动作 |
|---|---|
| `⌘K` | 命令面板 |
| `⌘N` | 新建对话 |
| `⌘U` | 上传文件 |
| `⌘\` | 收起/展开证据侧栏 |
| `⌘1~5` | 切换主导航页面 |
| `⌘Enter` | 发送消息 |
| `Esc` | 关闭浮层 / 停止生成 |

### 通知
用 `sonner` toast。摄取完成、进化完成、provider 失联降级都要提示。

---

## 6. 工程约定

```
src/
├── app/
│   ├── router.tsx
│   └── layouts/
├── features/
│   ├── chat/        { components/, hooks/, api.ts, types.ts }
│   ├── library/
│   ├── memory/
│   ├── evolve/
│   ├── expertise/
│   └── settings/
├── components/
│   ├── ui/          # shadcn 原子组件，不手改（升级会覆盖）
│   └── shared/      # 跨 feature 复用的业务组件
├── lib/
│   ├── api/
│   │   ├── client.ts      # fetch 封装 + 统一错误
│   │   ├── sse.ts         # SSE 订阅 hook（含中断、重连）
│   │   └── types.gen.ts   # ⚠️ 由 OpenAPI 生成，禁止手改
│   ├── format.ts
│   └── utils.ts
├── stores/
└── styles/
```

硬性规则
1. `types.gen.ts` 由 `pnpm gen:api`（openapi-typescript）从后端 `/openapi.json` 生成，任何手写 API 类型都算违规
2. 组件文件 ≤ 200 行；逻辑抽到 `hooks/`
3. 禁止 `any`；禁止在组件里直接 `fetch`（一律走 TanStack Query hook）
4. 所有颜色必须用语义 token（`bg-card` `text-muted-foreground`），禁止写 `bg-gray-800` 这类字面色，否则主题会崩
5. 每个页面必须实现四态：loading（骨架）/ empty（引导）/ error（可重试）/ ready
6. 移动端不做适配，但窗口 ≥ 1024px 时布局必须不塌

---

## 7. 验收标准

- [ ] 深浅两套主题下全部页面观感一致、无硬编码色
- [ ] 对话流式无抖动、代码块高亮正确、公式渲染正常
- [ ] 引用可点击并能定位到原文高亮处
- [ ] 上传 10 个文件时进度实时更新、互不阻塞
- [ ] 进化流水线动画流畅，数字有过渡
- [ ] `⌘K` 全局可用，键盘可完成：切 Space → 新建对话 → 发送
- [ ] Lighthouse 性能 ≥ 90；首屏 ≤ 1.5s（本地）
- [ ] `tsc --noEmit` 与 `eslint` 零报错
