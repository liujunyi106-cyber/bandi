/**
 * 功能：新建会话图标——左栏「新建会话」与项目面板
 * 头像行「＋新建会话」共用，保证同一语义同一图形。
 *
 * 形状：圆角气泡（r=4.5，介于 lucide `MessageSquarePlus` 的 r=2「偏方」与 `MessageCirclePlus`
 * 的「正圆」之间）+ 左下**开口**尾巴 + 加号；描边参数随顶栏左伸缩按钮
 * （24 viewBox / strokeWidth 1.75 / 圆头圆角 / currentColor）。
 * 注意：轮廓必须是**单条 path**——用 `<rect>` + 补线段拼会让尾巴变成外挂的斜线（闭合图元开不了洞）。
 */
export function NewSessionIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="2 2 20.5 20.5"
      className={className ?? "size-4 shrink-0"}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 12.5a4.5 4.5 0 0 1-4.5 4.5H7.5l-4.5 4.5V7.5a4.5 4.5 0 0 1 4.5-4.5h9a4.5 4.5 0 0 1 4.5 4.5z" />
      <path d="M12 7.5v5" />
      <path d="M9.5 10h5" />
    </svg>
  );
}
