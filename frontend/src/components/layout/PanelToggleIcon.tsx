/** 功能：面板开合图标（左/右栏共用同一线性图形与描边，保证按钮比例一致）。 */
export function PanelToggleIcon({
  side = "left",
  className = "size-4",
}: {
  side?: "left" | "right";
  className?: string;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      style={{ strokeWidth: 2 }}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d={side === "left" ? "M9 4v16" : "M15 4v16"} />
    </svg>
  );
}
