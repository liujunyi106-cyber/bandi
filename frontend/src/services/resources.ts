/**
 * 功能：资源中心 API（技能 / SOP 的列表、详情、保存、启停、卸载、上传）。
 */
import { api } from "@/services/api";

export type ResourceKind = "skill" | "sop";

export interface ResourceScope {
  global: boolean;
  employees: Array<{ id: string; name: string }>;
  team_id: string;
}

export interface ResourceItem {
  id: string;
  kind: ResourceKind;
  title: string;
  description: string;
  enabled: boolean;
  scope: ResourceScope;
  path: string;
  folder: string;
  updated_at: string;
  meta: Record<string, unknown>;
}

export interface ResourceDetailPayload {
  item: ResourceItem;
  content: string;
}

export const resourcesApi = {
  list: (kind: ResourceKind) =>
    api.get<{ items: ResourceItem[] }>(`/resources?kind=${encodeURIComponent(kind)}`),
  detail: (kind: ResourceKind, id: string) =>
    api.get<ResourceDetailPayload>(
      `/resources/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`,
    ),
  save: (kind: ResourceKind, id: string, content: string) =>
    api.put<{ ok: boolean; item: ResourceItem }>(
      `/resources/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`,
      { content },
    ),
  createSop: (content: string) =>
    api.post<{ ok: boolean; item: ResourceItem }>("/resources/sop", { content }),
  setEnabled: (kind: ResourceKind, id: string, enabled: boolean) =>
    api.patch<{ ok: boolean; item: ResourceItem }>(
      `/resources/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`,
      { enabled },
    ),
  uninstall: (kind: ResourceKind, id: string) =>
    api.del<{ ok: boolean; archived: string }>(
      `/resources/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`,
    ),
  openFolder: (kind: ResourceKind, id: string) =>
    api.post<{ ok: boolean; opened: string }>(
      `/resources/${encodeURIComponent(kind)}/${encodeURIComponent(id)}/open-folder`,
      {},
    ),
  upload: (kind: ResourceKind, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return api.postForm<{ ok: boolean; item: ResourceItem }>(
      `/resources/upload?kind=${encodeURIComponent(kind)}`,
      form,
    );
  },
};
