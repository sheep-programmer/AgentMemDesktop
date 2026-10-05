# 15 - 引用标记渲染一致性

现象：同一条回答里，前面的引用显示成灰色原文 `[c1]` / `[c2]`，后面的却是数字芯片
`3` —— 三种引用三种长相。

排查发现这是两条独立缺陷叠加的结果，都出在「引用数据晚于正文渲染」的时序上。

## 缺陷 1：streamdown 分块缓存按旧引用表渲染

`MarkdownViewImpl` 把 `[^cN]` 预处理成 `#cite:` 链接，再由 `a` 组件查
`citations` 决定渲染成数字芯片还是灰色兜底。processor 输出经 Streamdown 按块
memo：流式 mock 里正文（含裸标记）先到达、`citations` 最后才补，先渲染的块
被缓存住，闭包里还是空引用表 —— 于是完成后依然显示灰色 `[cN]`，而底部胶囊
（直接用 `message.citations`）却正常，同一条回答两种样式。

修复：`MarkdownViewImpl` 按引用标记集合计算 `citationKey`，集合变化时用
`key` 重挂载 Streamdown，所有块按最新引用表重算（引用一条回答只有个位数，
重挂载代价可忽略）。

## 缺陷 2：前端丢弃 citation 事件的 char_offset

后端流式协议是「正文剥离裸标记 + citation 事件携带 `char_offset`（标记被剥
离处的绝对下标）」，前端靠 `withCitationMarkers` 按位置把标记插回正文再渲染
芯片。但 `useChatStream` 的 `onCitation` 组装对象时漏掉了 `char_offset`
——真实流式期间 inline 芯片完全不出现，底部胶囊却有内容；刷新页面读历史
（落库的引用带 offset）芯片又冒出来。流式与历史两种长相。

修复：`onCitation` 保留 `data.char_offset`。

## 缺陷 3：mock 流式与生产契约不一致

mock 演示路径把裸 `[^cN]` 留在正文里、引用在结尾一次性补发，既触发缺陷 1，
也让演示效果与生产不一致（生产是边生成边出芯片）。

修复：mock 流式改为与后端相同的契约 —— 流出正文时剥离标记，行进到对应
位置时以 citation 事件（带 char_offset）逐个补发。

## 验证

- 新增 3 项回归：`MarkdownView.citations.test.tsx`（citations 晚到后全部统一成
  数字芯片；找不到真实引用的标记保持灰色原文不死链）、
  `useChatStream.test.ts`（citation 事件 char_offset 不丢失）。
- 浏览器实测（mock）：流式中途 chips `["1","2"]` 已内联出现，无裸标记、无灰色
  兜底；完成后 `["1","2","3"]`，种子历史会话同样统一。
- 前端全量 198 项测试通过，typecheck / lint / 生产构建 / 对比度检查通过。
- 截图：`docs/previews/citation-chips-uniform.png`。

## 角标样式

按用户反馈，正文内的引用芯片从大号内联胶囊改为右上角小角标（上标对齐、
9.5px、不撑行高），形态接近维基百科式脚注；悬停仍出出处预览，点击仍在证据栏
定位。底部「引用证据」胶囊行保持不变（那里承担的是来源列表，不是角标）。
截图：`docs/previews/citation-superscript.png`。

设计保留项：模型幻觉出证据表外的编号（如 `[^c9]`）时，仍按原设计显示灰色
原文而不渲染成点不开的死链芯片——那是「如实呈现模型写了什么」，不是缺陷。
