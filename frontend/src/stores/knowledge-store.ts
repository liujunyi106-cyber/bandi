/**
 * 功能：知识图谱搜索词全局态（顶栏搜索框与图谱页跨组件共享，不持久化）。
 */
import { create } from "zustand";

interface KnowledgeStoreState {
  /** 搜索词（顶栏输入、图谱页消费）。 */
  query: string;
  setQuery: (query: string) => void;
  resetQuery: () => void;
}

export const useKnowledgeStore = create<KnowledgeStoreState>((set) => ({
  query: "",
  setQuery: (query) => set({ query }),
  resetQuery: () => set({ query: "" }),
}));
