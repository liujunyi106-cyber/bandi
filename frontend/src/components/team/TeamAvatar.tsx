import { Users } from "lucide-react";

import { EmployeeAvatar } from "@/components/team/EmployeeAvatar";
import { cn } from "@/lib/utils";

export interface TeamAvatarMember {
  name: string;
  metadata?: Record<string, unknown> | null;
}

const CELL_2_3 = "size-[10px]";
const CELL_4 = "size-[9px]";

/** 功能：团队群聊头像——正方形轻圆角，内部最多 4 名员工头像。 */
export function TeamAvatar({
  members,
  name = "",
  fallback = "icon",
  className,
}: {
  members: TeamAvatarMember[];
  name?: string;
  fallback?: "icon" | "initial";
  className?: string;
}) {
  const visible = members.slice(0, 4);
  const count = visible.length;
  const cell = (member: TeamAvatarMember, index: number, sizeClass: string) => (
    <span
      key={`${member.name}-${index}`}
      data-testid="team-avatar-cell"
      className={cn("shrink-0 overflow-hidden rounded-[2px]", sizeClass)}
    >
      <EmployeeAvatar
        name={member.name}
        metadata={member.metadata}
        className="size-full rounded-none text-[6px]"
      />
    </span>
  );

  return (
    <span
      data-testid="team-avatar"
      data-count={count}
      aria-hidden="true"
      className={cn(
        "flex size-6 shrink-0 items-center justify-center overflow-hidden rounded-md",
        count === 0 && "bg-primary/5 text-primary",
        className,
      )}
    >
      {count === 0 ? (
        fallback === "initial" ? (
          <span className="text-chat-muted text-[10px] font-semibold">{name.slice(0, 1)}</span>
        ) : (
          <Users className="size-3" />
        )
      ) : count === 1 ? (
        <EmployeeAvatar
          name={visible[0].name}
          metadata={visible[0].metadata}
          className="size-full rounded-none text-[10px]"
        />
      ) : count === 2 ? (
        <span className="flex items-center justify-center gap-[1px]">
          {visible.map((member, index) => cell(member, index, CELL_2_3))}
        </span>
      ) : count === 3 ? (
        <span className="flex flex-col items-center justify-center gap-[1px]">
          <span data-testid="team-avatar-top" className="flex justify-center">
            {cell(visible[0], 0, CELL_2_3)}
          </span>
          <span data-testid="team-avatar-bottom" className="flex justify-center gap-[1px]">
            {cell(visible[1], 1, CELL_2_3)}
            {cell(visible[2], 2, CELL_2_3)}
          </span>
        </span>
      ) : (
        <span className="grid grid-cols-2 gap-[1px]">
          {visible.map((member, index) => cell(member, index, CELL_4))}
        </span>
      )}
    </span>
  );
}
