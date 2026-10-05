/** 新建会话时写死的占位标题。只有仍是它的会话才允许被自动改名，
 *  免得覆盖掉用户/后续逻辑设过的真标题。 */
export const DEFAULT_CONVERSATION_TITLE = '新对话';

const MAX_TITLE_LENGTH = 20;

/**
 * 从首条提问推导会话标题。
 *
 * 侧栏此前每一条都叫「新对话」——创建路径把标题写死，前后端都不会依据内容改名，
 * 也没有重命名入口，于是列表变成一排无法分辨的同名项。这个函数只做一件事：
 * 把提问的第一行清理成一个能认出来的短标题。
 *
 * 取不出任何可读内容时返回 `null`，由调用方决定保持占位标题不动——
 * 宁可不改名，也不要生成一个空标题或半个符号。
 */
export function deriveConversationTitle(text: string): string | null {
  const firstLine = text
    .split('\n')
    .map((line) => line.trim())
    // 跳过代码块围栏与空行，找第一行有内容的
    .find((line) => line.length > 0 && !line.startsWith('```'));

  if (!firstLine) return null;

  const cleaned = firstLine
    // 去掉标题井号、引用角括号、列表符号等行首装饰
    .replace(/^[#>\-*+\s]+/, '')
    // 去掉行内代码/强调标记，保留文字本身
    .replace(/[`*_~]/g, '')
    .replace(/\s+/g, ' ')
    .trim();

  if (!cleaned) return null;

  return cleaned.length > MAX_TITLE_LENGTH
    ? `${cleaned.slice(0, MAX_TITLE_LENGTH)}…`
    : cleaned;
}
