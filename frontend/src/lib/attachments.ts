/**
 * 功能：附件上传消费（Composer 用；立即上传 + store 三态管理——Day137b-2）。
 */
import { companyApi } from "@/services/company";
import { useChatStore, type ChatAttachment } from "@/stores/chat-store";

/** 功能：批量上传文件（立即上传；逐文件三态：uploading→done/error）。 */
export async function uploadAttachments(files: File[], projectId: string): Promise<void> {
  const store = useChatStore.getState();
  for (const file of files) {
    const tempId = `pending:${Date.now()}:${Math.random().toString(16).slice(2, 8)}`;
    store.addDraftAttachment({
      upload_id: tempId,
      name: file.name,
      size: file.size,
      status: "uploading",
    });
    try {
      const { upload } = await companyApi.upload(file, projectId);
      store.updateDraftAttachment(tempId, {
        upload_id: upload.upload_id,
        name: upload.name,
        size: upload.size,
        status: "done",
      });
    } catch {
      store.updateDraftAttachment(tempId, { status: "error" });
    }
  }
}

/** 功能：可发送附件引用（仅 done 态——发送 payload 数据源）。 */
export function readyAttachments(): ChatAttachment[] {
  return useChatStore
    .getState()
    .draftAttachments.filter((item) => item.status === "done")
    .map((item) => ({ upload_id: item.upload_id, name: item.name, size: item.size }));
}

/** 功能：字节数 → 可读大小（B/KB/MB）。 */
export function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
