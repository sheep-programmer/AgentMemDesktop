import { toast } from 'sonner';
import type { UploadResult } from '@/lib/api/services/documents';

/**
 * 多文件上传的结果提示：收下了几个、哪几个没收下、为什么。
 *
 * 后端逐个文件处理，一批里有一个重复不会拖累其余。提示也得分开说，
 * 否则要么「全部成功」掩盖了被拒的，要么「上传失败」让人以为一个都没进来。
 */
export function reportUploadResult(result: UploadResult): void {
  const { count, rejected } = result;
  if (count > 0) {
    toast.success(`已导入 ${count} 个文件，正在后台处理`);
  }
  if (rejected.length > 0) {
    toast.warning(`${rejected.length} 个文件没有导入`, {
      description: rejected.map((item) => `${item.filename}：${item.message}`).join('\n'),
      duration: 10000,
    });
  }
}
