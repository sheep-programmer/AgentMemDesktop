import React, { useState, useCallback, useEffect, useId } from 'react';
import { useDropzone, type FileRejection } from 'react-dropzone';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Button } from '@/components/ui/button';
import { UploadCloud, Globe, Clipboard, Loader2 } from 'lucide-react';
import { documentService } from '@/lib/api';
import type { UploadRejection } from '@/lib/api/services/documents';
import { useAsyncTask } from '@/hooks/useAsyncTask';
import { toast } from 'sonner';
import { reportUploadResult } from '../lib/uploadToast';

interface DocumentUploadModalProps {
  spaceId: string;
  isOpen: boolean;
  onClose: () => void;
  onUploaded: () => void;
}

// Matches the API upload limit. Formats remain decided by the backend parsers.
const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;
const FIELD_STYLE =
  'w-full rounded-lg border border-input bg-background px-3 py-2.5 text-sm text-foreground outline-none focus-visible:border-primary focus-visible:ring-2 focus-visible:ring-primary/20 disabled:opacity-60';

export function DocumentUploadModal({
  spaceId,
  isOpen,
  onClose,
  onUploaded,
}: DocumentUploadModalProps) {
  const id = useId();
  const [pasteText, setPasteText] = useState('');
  const [pasteTitle, setPasteTitle] = useState('');
  const [importUrl, setImportUrl] = useState('');
  const [urlTitle, setUrlTitle] = useState('');
  const [isUploading, setIsUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fileIssues, setFileIssues] = useState<UploadRejection[]>([]);
  const [urlError, setUrlError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState('file');
  const { start: startTask } = useAsyncTask(isOpen ? spaceId : null);

  useEffect(() => {
    setIsUploading(false);
    setError(null);
    setFileIssues([]);
    setUrlError(null);
  }, [isOpen, spaceId]);
  useEffect(() => {
    setPasteText('');
    setPasteTitle('');
    setImportUrl('');
    setUrlTitle('');
    setActiveTab('file');
  }, [spaceId]);

  const onDrop = useCallback(
    async (acceptedFiles: File[], rejectedFiles: FileRejection[]) => {
      if (!isOpen || !spaceId) return;
      const localIssues = rejectedFiles.map(({ file, errors }) => ({
        filename: file.name,
        code: errors[0]?.code || 'FILE_REJECTED',
        message: errors.some((item) => item.code === 'file-too-large')
          ? '文件超过 100 MB，请拆分或压缩后再导入'
          : '文件无法读取，请重新选择',
      }));
      if (acceptedFiles.length === 0) {
        setFileIssues(localIssues);
        return;
      }
      const task = startTask();
      if (!task) return;
      setIsUploading(true);
      setError(null);
      setFileIssues(localIssues);
      try {
        const result = await documentService.uploadFiles(
          spaceId,
          acceptedFiles,
        );
        if (!task.current()) return;
        const rejected = [...localIssues, ...result.rejected];
        reportUploadResult({ ...result, rejected });
        setFileIssues(rejected);
        onUploaded();
        if (rejected.length === 0) onClose();
      } catch (err) {
        if (task.current()) {
          setError(err instanceof Error ? err.message : '导入失败，请重试');
          onUploaded();
        }
      } finally {
        if (task.finish()) setIsUploading(false);
      }
    },
    [spaceId, isOpen, startTask, onUploaded, onClose],
  );

  const { getRootProps, getInputProps, isDragActive, open } = useDropzone({
    onDrop,
    multiple: true,
    maxSize: MAX_UPLOAD_BYTES,
    disabled: isUploading,
    noClick: true,
    noKeyboard: true,
  });

  const handlePasteSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!isOpen || !spaceId || !pasteText.trim()) return;
    const task = startTask();
    if (!task) return;
    const title =
      pasteTitle.trim() || `文本笔记 ${new Date().toLocaleDateString()}`;
    setIsUploading(true);
    setError(null);
    try {
      await documentService.pasteText(spaceId, title, pasteText);
      if (!task.current()) return;
      toast.success('文本已导入，正在后台处理');
      onUploaded();
      setPasteText('');
      setPasteTitle('');
      onClose();
    } catch (err) {
      if (task.current())
        setError(err instanceof Error ? err.message : '文本导入失败，请重试');
    } finally {
      if (task.finish()) setIsUploading(false);
    }
  };

  const handleUrlSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!isOpen || !spaceId || !importUrl.trim()) return;
    let url: URL;
    try {
      url = new URL(importUrl.trim());
      if (!['http:', 'https:'].includes(url.protocol))
        throw new Error('protocol');
    } catch {
      setUrlError('请输入完整的网页地址，以 https:// 或 http:// 开头');
      return;
    }
    const task = startTask();
    if (!task) return;
    setUrlError(null);
    setError(null);
    setIsUploading(true);
    try {
      await documentService.ingestUrl(
        spaceId,
        url.href,
        urlTitle.trim() || undefined,
      );
      if (!task.current()) return;
      toast.success('网页已提交，正在后台处理');
      onUploaded();
      setImportUrl('');
      setUrlTitle('');
      onClose();
    } catch (err) {
      if (task.current())
        setError(err instanceof Error ? err.message : '网页导入失败，请重试');
    } finally {
      if (task.finish()) setIsUploading(false);
    }
  };

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(opened) => {
        if (!opened) onClose();
      }}
    >
      <DialogContent className="w-[calc(100%-2rem)] max-w-lg max-h-[calc(100dvh-2rem)] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>导入资料</DialogTitle>
          <DialogDescription>
            添加文件、文本或网页，处理完成后即可用于检索和引用。
          </DialogDescription>
        </DialogHeader>
        {error && (
          <p
            role="alert"
            className="break-words rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"
          >
            {error}
            <span className="mt-1 block text-xs">
              请检查后重试，或关闭弹窗查看资料库。
            </span>
          </p>
        )}
        <Tabs
          value={activeTab}
          onValueChange={(value) => {
            if (!isUploading) {
              setActiveTab(String(value));
              setError(null);
            }
          }}
          className="w-full"
        >
          <TabsList className="grid w-full grid-cols-3">
            <TabsTrigger
              disabled={isUploading}
              value="file"
              className="min-h-9 gap-1 text-xs"
            >
              <UploadCloud className="h-3.5 w-3.5" />
              本地文件
            </TabsTrigger>
            <TabsTrigger
              disabled={isUploading}
              value="paste"
              className="min-h-9 gap-1 text-xs"
            >
              <Clipboard className="h-3.5 w-3.5" />
              粘贴文本
            </TabsTrigger>
            <TabsTrigger
              disabled={isUploading}
              value="url"
              className="min-h-9 gap-1 text-xs"
            >
              <Globe className="h-3.5 w-3.5" />
              网页链接
            </TabsTrigger>
          </TabsList>

          <TabsContent value="file" className="mt-4 space-y-3">
            <div
              {...getRootProps({
                role: 'region',
                'aria-label': '文件拖放区域',
              })}
              className={`flex flex-col items-center rounded-xl border-2 border-dashed px-4 py-6 text-center ${isDragActive ? 'border-primary bg-primary/5' : 'border-border bg-muted/20'}`}
            >
              <input {...getInputProps({ 'aria-label': '选择资料文件' })} />
              <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-primary/10 text-primary">
                {isUploading ? (
                  <Loader2 className="h-6 w-6 animate-spin" />
                ) : (
                  <UploadCloud className="h-6 w-6" />
                )}
              </div>
              <p className="text-sm font-medium">
                {isUploading ? '正在上传资料…' : '将文件拖到这里'}
              </p>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                PDF、Markdown、Word、TXT 等常见格式
                <br />
                可一次选择多个文件，单个文件不超过 100 MB
              </p>
              <Button
                type="button"
                variant="outline"
                onClick={open}
                disabled={isUploading}
                className="mt-4 min-h-10"
              >
                选择文件
              </Button>
            </div>
            {fileIssues.length > 0 && (
              <div
                role="alert"
                className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-xs"
              >
                <p className="font-medium text-destructive">
                  以下 {fileIssues.length} 个文件没有导入
                </p>
                <ul className="mt-2 max-h-40 space-y-2 overflow-y-auto">
                  {fileIssues.map((issue, index) => (
                    <li key={index} className="break-words text-foreground">
                      <span className="font-medium">{issue.filename}</span>
                      <p className="mt-0.5 text-muted-foreground">
                        {issue.message}
                      </p>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </TabsContent>

          <TabsContent value="paste" className="mt-4">
            <form onSubmit={handlePasteSubmit} className="space-y-4">
              <div>
                <label
                  htmlFor={`${id}-paste-title`}
                  className="mb-1.5 block text-sm font-medium"
                >
                  资料标题{' '}
                  <span className="text-xs font-normal text-muted-foreground">
                    （可选）
                  </span>
                </label>
                <input
                  id={`${id}-paste-title`}
                  disabled={isUploading}
                  value={pasteTitle}
                  onChange={(event) => setPasteTitle(event.target.value)}
                  placeholder="例如：项目会议记录"
                  className={FIELD_STYLE}
                />
              </div>
              <div>
                <label
                  htmlFor={`${id}-paste-text`}
                  className="mb-1.5 block text-sm font-medium"
                >
                  文本正文
                </label>
                <textarea
                  id={`${id}-paste-text`}
                  disabled={isUploading}
                  rows={6}
                  value={pasteText}
                  onChange={(event) => setPasteText(event.target.value)}
                  onKeyDown={(event) => {
                    if (
                      (event.metaKey || event.ctrlKey) &&
                      event.key === 'Enter' &&
                      !event.nativeEvent.isComposing
                    ) {
                      event.preventDefault();
                      event.currentTarget.form?.requestSubmit();
                    }
                  }}
                  placeholder="粘贴笔记、会议记录或文档内容…"
                  className={`${FIELD_STYLE} min-h-32 resize-y`}
                />
                <p className="mt-1.5 text-xs text-muted-foreground">
                  保留原有段落和标题，有助于定位引用。可按 Ctrl / ⌘ + Enter
                  导入。
                </p>
              </div>
              <div className="flex justify-end gap-2">
                <Button type="button" variant="ghost" onClick={onClose}>
                  关闭
                </Button>
                <Button
                  type="submit"
                  disabled={!pasteText.trim() || isUploading}
                  className="min-h-10 gap-1.5"
                >
                  {isUploading && <Loader2 className="h-4 w-4 animate-spin" />}
                  {isUploading ? '正在导入…' : '导入文本'}
                </Button>
              </div>
            </form>
          </TabsContent>

          <TabsContent value="url" className="mt-4">
            <form noValidate onSubmit={handleUrlSubmit} className="space-y-4">
              <div>
                <label
                  htmlFor={`${id}-url`}
                  className="mb-1.5 block text-sm font-medium"
                >
                  网页地址
                </label>
                <input
                  id={`${id}-url`}
                  type="url"
                  inputMode="url"
                  autoComplete="url"
                  spellCheck={false}
                  disabled={isUploading}
                  value={importUrl}
                  onChange={(event) => {
                    setImportUrl(event.target.value);
                    setUrlError(null);
                  }}
                  placeholder="https://example.com/article"
                  aria-invalid={Boolean(urlError)}
                  aria-describedby={`${id}-url-help`}
                  className={FIELD_STYLE}
                />
                <p
                  id={`${id}-url-help`}
                  role={urlError ? 'alert' : undefined}
                  className={`mt-1.5 text-xs leading-relaxed ${urlError ? 'text-destructive' : 'text-muted-foreground'}`}
                >
                  {urlError ||
                    '填写可直接访问的网页。登录后才能查看的内容，建议复制为文本导入。'}
                </p>
              </div>
              <div>
                <label
                  htmlFor={`${id}-url-title`}
                  className="mb-1.5 block text-sm font-medium"
                >
                  资料标题{' '}
                  <span className="text-xs font-normal text-muted-foreground">
                    （可选）
                  </span>
                </label>
                <input
                  id={`${id}-url-title`}
                  disabled={isUploading}
                  value={urlTitle}
                  onChange={(event) => setUrlTitle(event.target.value)}
                  placeholder="留空时使用网页标题"
                  className={FIELD_STYLE}
                />
              </div>
              <div className="flex justify-end gap-2">
                <Button type="button" variant="ghost" onClick={onClose}>
                  关闭
                </Button>
                <Button
                  type="submit"
                  disabled={!importUrl.trim() || isUploading}
                  className="min-h-10 gap-1.5"
                >
                  {isUploading && <Loader2 className="h-4 w-4 animate-spin" />}
                  {isUploading ? '正在提交…' : '导入网页'}
                </Button>
              </div>
            </form>
          </TabsContent>
        </Tabs>
        {isUploading && (
          <p
            role="status"
            className="text-xs leading-relaxed text-muted-foreground"
          >
            提交后会在后台解析。关闭弹窗不会撤回已提交的资料，可以回资料库查看状态。
          </p>
        )}
      </DialogContent>
    </Dialog>
  );
}
