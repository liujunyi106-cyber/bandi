/**
 * 功能：文本复制的统一入口——先走标准 API，失败退回选区复制。
 *
 * 为什么要降级：`navigator.clipboard.writeText` 只在安全上下文（https / localhost）
 * 且窗口有焦点时可用。Electron 打包后页面来自自定义协议、被托盘隐藏的窗口也没有
 * 焦点，此时它会直接 reject，界面表现为「点了复制没反应」。
 */

/** 功能：复制文本到剪贴板，返回是否成功。 */
export async function copyText(text: string): Promise<boolean> {
  // 不预判 isSecureContext：它在部分运行时为 undefined，预判会把可用的 API 误判成不可用。
  // 直接调，失败（reject）再降级——列举法进行试错而不是预设条件。
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // 继续走降级路径
  }
  return copyBySelection(text);
}

/** 功能：用临时只读文本域 + execCommand 复制（非安全上下文下的唯一可用路径）。 */
function copyBySelection(text: string): boolean {
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    area.setSelectionRange(0, area.value.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}
