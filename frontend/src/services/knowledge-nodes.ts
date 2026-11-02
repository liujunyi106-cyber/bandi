/**
 * 功能：资料库节点数据层——树查询、新建、改名/移动、上传、内容读写。
 * 后端契约见 `bandi/api/knowledge_nodes_api.py`（GET/POST/PATCH/DELETE + upload/content/preview）。
 */
import { desktopApiKey } from "@/lib/desktop";
import { api, ApiError } from "@/services/api";

export type KnowledgeNodeKind =
  | "folder"
  | "md"
  | "csv"
  | "docx"
  | "xlsx"
  | "pptx"
  | "pdf"
  | "html"
  | "link";

export interface KnowledgeNode {
  id: string;
  base_id: string;
  parent_id: string;
  kind: KnowledgeNodeKind;
  name: string;
  sort_order: number;
  employee_id: string | null;
  size: number;
  url: string;
  updated_at: number;
}

export interface KnowledgeNodeContent {
  kind: string;
  text?: string;
  base_mtime?: number;
  pdf_url?: string;
  html?: string;
  url?: string;
  snapshot_html?: string;
  snapshot_at?: number;
}

export interface KnowledgeBaseInfo {
  base_id: string;
  name: string;
  employee_id: string;
  document_count: number;
}

export const knowledgeNodesApi = {
  async bases(): Promise<KnowledgeBaseInfo[]> {
    const data = await api.get<{ bases: KnowledgeBaseInfo[] }>("/company/knowledge/bases");
    return data.bases ?? [];
  },

  async list(baseId: string, parentId = "", q = ""): Promise<KnowledgeNode[]> {
    const params = new URLSearchParams({ base_id: baseId });
    if (parentId) params.set("parent_id", parentId);
    if (q) params.set("q", q);
    const data = await api.get<{ nodes: KnowledgeNode[] }>(`/knowledge/nodes?${params}`);
    return data.nodes ?? [];
  },

  async listAll(baseId: string): Promise<KnowledgeNode[]> {
    const data = await api.get<{ nodes: KnowledgeNode[] }>(
      `/knowledge/nodes?base_id=${encodeURIComponent(baseId)}&flat=1`,
    );
    return data.nodes ?? [];
  },

  create(input: {
    baseId: string;
    parentId?: string;
    kind: KnowledgeNodeKind;
    name: string;
    url?: string;
  }): Promise<KnowledgeNode> {
    return api.post<KnowledgeNode>("/knowledge/nodes", {
      base_id: input.baseId,
      parent_id: input.parentId ?? "",
      kind: input.kind,
      name: input.name,
      url: input.url ?? "",
    });
  },

  patch(
    nodeId: string,
    input: { name?: string; parentId?: string; employeeId?: string | null },
  ): Promise<KnowledgeNode> {
    return api.patch<KnowledgeNode>(`/knowledge/nodes/${nodeId}`, {
      ...(input.name !== undefined ? { name: input.name } : {}),
      ...(input.parentId !== undefined ? { parent_id: input.parentId } : {}),
      ...(input.employeeId !== undefined ? { employee_id: input.employeeId } : {}),
    });
  },

  remove(nodeId: string): Promise<{ deleted_nodes: number; deleted_documents: number }> {
    return api.del(`/knowledge/nodes/${nodeId}`);
  },

  /** 功能：带进度/可取消的上传——根目录或指定文件夹（XHR 才能拿到上传进度）。 */
  uploadWithProgress(input: {
    baseId: string;
    parentId: string;
    file: File;
    onProgress?: (ratio: number) => void;
  }): { promise: Promise<KnowledgeNode>; abort: () => void } {
    const xhr = new XMLHttpRequest();
    const promise = new Promise<KnowledgeNode>((resolve, reject) => {
      const form = new FormData();
      form.append("base_id", input.baseId);
      form.append("parent_id", input.parentId);
      form.append("file", input.file);
      xhr.open("POST", "/api/knowledge/nodes/upload");
      xhr.setRequestHeader("Accept", "application/json");
      const apiKey = desktopApiKey();
      if (apiKey) xhr.setRequestHeader("Authorization", `Bearer ${apiKey}`);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && event.total > 0) {
          input.onProgress?.(event.loaded / event.total);
        }
      };
      xhr.onerror = () => reject(new ApiError(0, "内核未就绪或网络不可达"));
      xhr.onabort = () => reject(new ApiError(0, "上传已取消"));
      xhr.onload = () => {
        let parsed: unknown;
        try {
          parsed = xhr.responseText ? JSON.parse(xhr.responseText) : undefined;
        } catch {
          parsed = undefined;
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(parsed as KnowledgeNode);
          return;
        }
        const detail =
          parsed && typeof parsed === "object" && "detail" in parsed
            ? String((parsed as { detail?: unknown }).detail)
            : `上传失败（HTTP ${xhr.status}）`;
        reject(new ApiError(xhr.status, detail, parsed as never));
      };
      xhr.send(form);
    });
    return { promise, abort: () => xhr.abort() };
  },

  /** 功能：显式重建索引——编辑停手/切换文件时才调用。 */
  ingest(nodeId: string): Promise<{ indexed: boolean; document_id: string | null }> {
    return api.post(`/knowledge/nodes/${nodeId}/ingest`);
  },

  uploadAsset(
    nodeId: string,
    file: File,
  ): Promise<{ name: string; size: number; url: string }> {
    const form = new FormData();
    form.append("file", file);
    return api.postForm(`/knowledge/nodes/${nodeId}/assets`, form);
  },

  content(nodeId: string): Promise<KnowledgeNodeContent> {
    return api.get<KnowledgeNodeContent>(`/knowledge/nodes/${nodeId}/content`);
  },

  saveText(
    nodeId: string,
    text: string,
    baseMtime?: number,
    ingest = true,
    force = false,
  ): Promise<{ saved_at: number; warning?: string | null }> {
    return api.put(`/knowledge/nodes/${nodeId}/content`, {
      text,
      base_mtime: baseMtime,
      ingest,
      force,
    });
  },

  /** 功能：链接节点手动抓取正文快照（服务端公网抓取 + 清洗 + ingest）。 */
  snapshot(nodeId: string): Promise<{
    ok: boolean;
    title: string;
    bytes: number;
    snapshot_at: number;
    scheduled: boolean;
    document_id?: string | null;
  }> {
    return api.post(`/knowledge/nodes/${nodeId}/snapshot`, {});
  },

  viewState(nodeId: string): Promise<{ state: Record<string, unknown> }> {
    return api.get(`/knowledge/nodes/${nodeId}/view-state`);
  },

  saveViewState(nodeId: string, state: Record<string, unknown>): Promise<{ saved: boolean }> {    return api.put(`/knowledge/nodes/${nodeId}/view-state`, { state });
  },

  listSheets(nodeId: string): Promise<{ sheets: string[] }> {
    return api.get(`/knowledge/nodes/${nodeId}/sheets`);
  },

  slides(
    nodeId: string,
  ): Promise<{ count: number; slides: { index: number; title: string; text: string }[] }> {
    return api.get(`/knowledge/nodes/${nodeId}/slides`);
  },

  sheetRows(
    nodeId: string,
    sheet: string,
    limit = 200,
  ): Promise<{ name: string; rows: string[][]; truncated: boolean }> {
    return api.get(
      `/knowledge/nodes/${nodeId}/sheets/${encodeURIComponent(sheet)}?limit=${limit}`,
    );
  },

  downloadUrl(nodeId: string): string {
    return `/api/knowledge/nodes/${nodeId}/download`;
  },
};
