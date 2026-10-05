import type { DocumentItem } from '@/lib/api/types.temp';

export interface StarterPrompt {
  title: string;
  desc: string;
  query: string;
}

const MAX_PROMPTS = 3;
const DESC_CHARS = 60;

/** 去掉文件扩展名：「稳定性数据.md」→「稳定性数据」 */
function displayTitle(title: string): string {
  return title.replace(/\.(md|markdown|txt|pdf|docx?|html?)$/i, '').trim() || title;
}

/**
 * 起手问题从这个空间真实的资料里来。
 *
 * 此前这里写死了三条医药问题（EGFR T790M、ADMET、PROTAC），新建的任何空间——
 * 哪怕是校园助手——一进来都推荐问靶点突变。没有资料就不推荐，
 * 顶上的「尚未导入资料」提示条会引导去导入。
 */
export function buildStarterPrompts(documents: DocumentItem[]): StarterPrompt[] {
  return documents
    .filter((doc) => doc.status === 'ready' && doc.title)
    .slice(0, MAX_PROMPTS)
    .map((doc) => {
      const title = displayTitle(doc.title);
      const summary = (doc.meta?.context_summary ?? '').trim();
      return {
        title,
        desc: summary
          ? summary.length > DESC_CHARS
            ? `${summary.slice(0, DESC_CHARS)}…`
            : summary
          : `知识库中的《${title}》`,
        query: `《${title}》主要讲了什么？有哪些要点？`,
      };
    });
}
