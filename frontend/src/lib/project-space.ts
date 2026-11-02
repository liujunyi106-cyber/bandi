/**
 * 功能：项目空间工具（名称 ↔ 标识/路径派生；「新建项目空间」与「打开本地文件夹」共用）。
 * 单一来源，避免两个入口各自写一份清洗规则（后端 validate_path_segment 为最终校验）。
 */

/**
 * 功能：把显示名清洗为路径安全的 project_id（保留字母/数字/下划线/连字符，含中日韩文字）。
 * 注意：JS 的 `\w` 只含 ASCII，中文名会被整体吃掉——必须用 Unicode 属性转义
 * （与后端 Python `validate_path_segment` 的 `[\w-]` 口径对齐，否则中文项目空间名拿不到同名 id）。
 */
export function projectIdFromName(name: string, fallback = ""): string {
  const cleaned = name
    .trim()
    .replace(/[^\p{L}\p{N}_-]+/gu, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
  return cleaned || fallback;
}

/** 功能：取路径末段作为项目空间名（Windows/POSIX 分隔符通吃）。 */
export function nameFromPath(path: string, fallback = ""): string {
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return parts[parts.length - 1] || fallback;
}

/** 功能：判断路径是否可登记为项目空间（盘符根/文件系统根不可——无法生成同名文件夹语义）。 */
export function isPickableFolder(path: string): boolean {
  const value = path.trim();
  if (!value) return false;
  return !/^([A-Za-z]:[\\/]*|\/)$/.test(value);
}
