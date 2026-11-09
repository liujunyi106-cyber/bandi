/**
 * 功能：空员工状态引导。
 * 组织还没有员工时，群聊/单聊/会话空态展示 → 组织面板（团队页）创建员工。
 */
import { UserRoundPlus } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";

export function EmptyEmployeeState() {
  const t = useT();
  const navigate = useNavigate();

  return (
    <div
      className="text-chat-muted flex min-h-0 flex-1 flex-col items-center justify-center gap-3"
      data-testid="empty-employee-state"
    >
      <UserRoundPlus className="size-8 opacity-40" />
      <p className="max-w-xs text-center text-sm">{t("chat.emptyEmployees.hint")}</p>
      <Button size="sm" className="h-7 px-3 text-xs" onClick={() => navigate("/team")}>
        {t("chat.emptyEmployees.action")}
      </Button>
    </div>
  );
}
