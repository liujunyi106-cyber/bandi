/**
 * 功能：provider → 官方名（配置弹窗下拉/字段展示用； ：
 * 列表项 = logo + 单名——有官方中文名用中文，无则保留原名，不做中英并列备注）。
 */
export const PROVIDER_LABELS: Record<string, string> = {
  deepseek: "深度求索",
  openai: "OpenAI",
  anthropic: "Anthropic",
  gemini: "Google Gemini",
  moonshot: "月之暗面",
  zhipu: "智谱",
  minimax: "MiniMax",
  mimo: "小米 MiMo",
  ollama: "Ollama",
  openai_compat: "兼容接口",
  mock: "Mock",
};

/** 官方供应商清单（全段字母自动排序——新增项按 localeCompare 归位）。 */
export const OFFICIAL_PROVIDERS: readonly string[] = [
  "anthropic",
  "deepseek",
  "gemini",
  "mimo",
  "minimax",
  "moonshot",
  "ollama",
  "openai",
  "openai_compat",
  "zhipu",
];

export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}
