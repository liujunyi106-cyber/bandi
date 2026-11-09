import type { CSSProperties } from "react";

import beanSvg from "@/assets/avatar-shapes/bean.svg?raw";
import capsuleSvg from "@/assets/avatar-shapes/capsule.svg?raw";
import cloudSvg from "@/assets/avatar-shapes/cloud.svg?raw";
import dangoSvg from "@/assets/avatar-shapes/dango.svg?raw";
import eggSvg from "@/assets/avatar-shapes/egg.svg?raw";
import leafSvg from "@/assets/avatar-shapes/leaf.svg?raw";
import waterdropSvg from "@/assets/avatar-shapes/waterdrop.svg?raw";
import type { AvatarShapeId } from "@/lib/employee-avatar";
import { cn } from "@/lib/utils";

const SHAPE_SVGS: Record<Exclude<AvatarShapeId, "classic">, string> = {
  capsule: capsuleSvg,
  cloud: cloudSvg,
  egg: eggSvg,
  bean: beanSvg,
  waterdrop: waterdropSvg,
  leaf: leafSvg,
  dango: dangoSvg,
};

/** 功能：按当前文字颜色渲染员工头像形状（SVG 已裁掉透明留白）。 */
export function AvatarShape({
  shape,
  className,
  style,
}: {
  shape: AvatarShapeId;
  className?: string;
  style?: CSSProperties;
}) {
  if (shape === "classic") return null;
  return (
    <span
      aria-hidden="true"
      className={cn("[&>svg]:block [&>svg]:size-full", className)}
      style={style}
      dangerouslySetInnerHTML={{ __html: SHAPE_SVGS[shape] }}
    />
  );
}
