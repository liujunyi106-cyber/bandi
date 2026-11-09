/**
 * 功能：SOP 驳回/返工边（从右侧绕行的虚线，避免与正向流程重叠）。
 */
import { BaseEdge, EdgeLabelRenderer, type EdgeProps } from "@xyflow/react";

const LANE_GAP = 72;
const CORNER_RADIUS = 10;
const STUB = 22;

export function SopReworkEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  markerEnd,
  style,
  label,
}: EdgeProps) {
  const laneX = Math.max(sourceX, targetX) + LANE_GAP;
  const downY = sourceY + STUB;
  const upY = targetY - STUB;
  const path = [
    `M ${sourceX} ${sourceY}`,
    `L ${sourceX} ${downY}`,
    `L ${laneX - CORNER_RADIUS} ${downY}`,
    `Q ${laneX} ${downY} ${laneX} ${downY + CORNER_RADIUS}`,
    `L ${laneX} ${upY - CORNER_RADIUS}`,
    `Q ${laneX} ${upY} ${laneX - CORNER_RADIUS} ${upY}`,
    `L ${targetX} ${upY}`,
    `L ${targetX} ${targetY}`,
  ].join(" ");

  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} />
      {label ? (
        <EdgeLabelRenderer>
          <div
            className="text-chat-muted pointer-events-none absolute rounded bg-[#f7f8fa] px-1 text-[10px]"
            style={{
              transform: `translate(-50%, -50%) translate(${laneX + 22}px, ${(downY + upY) / 2}px)`,
            }}
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}
