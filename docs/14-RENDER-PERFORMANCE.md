# 14 - 流式渲染性能优化

本轮针对聊天页在流式回答期间的重渲染开销做了一轮测量驱动的优化。
此前的链路是：每个 token 一次 `setMessages` → ChatPage 整树重渲染 →
每条已完成的 Markdown 气泡跟着重算（表格、代码高亮、引用标记全部重来一遍）。

## 改动

### 1. 流式增量合并写入（`useChatStream`)

token 到达频率远高于屏幕刷新率（mock 逐字 18ms，真实后端按网络分片突发）。
新增 `createStreamWriter`：把同一窗口内的增量合并，最多每 50ms 落一次 state。
视觉上看不出差别，状态更新次数从「每 token 一次」封顶到「每秒约 20 次」。

终止路径全部显式 `flushNow()`，保证合并不会丢内容：

- `onDone` / `onError`：先把缓冲落地，再写回 trace、用量等收尾状态；
- 断流（EOF 无终止事件）：先落地，再追加「回答未完成」提示；
- 请求异常：先落地，再把气泡替换为错误说明（行为与之前一致）；
- 手动停止：`abortStream` 先把最后几十毫秒的缓冲文本落地，再作废弃流——
  停止时不再丢掉最后一段回答；
- 取消 / 提前返回：`cancel()` 清掉挂起的定时器，旧流的迟到 flush 由
  `isCurrent()` 守卫挡掉。

### 2. 消息气泡 memo 化（`ChatMessageItem`)

`React.memo` 包裹。流式期间只有正在生成的那条气泡（`message` 对象变化）重渲染，
历史气泡的 Markdown 不再跟着每个时间片重算。

### 3. 回调身份稳定（`ChatPage`)

memo 生效的前提是回调引用不变：

- `onRegenerate` 依赖 `messages`/`isStreaming`，直接 `useCallback` 会每帧换引用，
  改为 ref 转发（`stableRegenerate`）；
- `onConversationsLoaded` / `onConversationRenamed` / 抽屉版 `onSelect`
  改为稳定回调，配合会话列表的 memo。

### 4. 会话列表 memo 化（`ConversationList`)

流式期间 ChatPage 每次 state 更新不再带着整列会话一起重渲染。
（组件内部早已用 ref 兜住回调身份，见 `ConversationList.tsx` 顶部注释，
此处是把渲染本身也省掉。）

## 实测

mock 流式回答（约 340 字符，逐字到达）：

| 指标 | 优化前 | 优化后 |
| --- | --- | --- |
| state 更新频率 | 每 token 一次（约 55 次/秒） | ≤ 20 次/秒 |
| 回答期间 DOM 变更批次数（MutationObserver） | 约每字符一批 | 7.1 秒共 80 批（≈11 次/秒） |
| 历史 Markdown 气泡 | 每 token 全量重渲染 | 零重渲染（memo 跳过） |

浏览器验收（mock 数据）：流式回答逐段出现、引用与操作按钮完整、
截图存档于 `docs/previews/stream-batching.png`。

## 回归测试

`useChatStream.test.ts` 新增 2 项：

1. 突发 delta 合并：同一时间片里的 3 个 delta 只产生一次 state 更新，
   时间片结束后文本完整拼接；
2. 手动停止保留缓冲末尾：`abortStream` 后消息里保留已缓冲的部分回答。

## 验证

- 前端全量 195 项测试通过（38 个文件）
- typecheck / lint / 生产构建通过（>500KB 的按需 chunk 警告维持原状，属预期）
- 视觉验证：聊天页渲染、引用、操作按钮、证据栏均正常
