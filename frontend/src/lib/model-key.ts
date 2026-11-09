/**
 * 功能：模型键归一化（网关命名空间去前缀——`xiaomi/mimo-v2.6-flash` → `mimo-v2.6-flash`）。
 * 与后端 ModelCatalog.get 的「未命中且含 / 取末段」归一一致；档案模型与目录键的形态差异由此抹平。
 */
export function normalizeModelKey(model: string): string {
  const trimmed = model.trim();
  if (!trimmed.includes("/")) return trimmed;
  return trimmed.slice(trimmed.lastIndexOf("/") + 1);
}
