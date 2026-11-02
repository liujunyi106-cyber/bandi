/**
 * 功能：空状态员工选择器——只列出可发起单聊的在职员工，选中后由 Composer 显示胶囊。
 */
import { Search } from "lucide-react";
import { useMemo, useState } from "react";

import { EmployeeAvatar } from "@/components/team/EmployeeAvatar";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useT } from "@/i18n";
import type { EmployeeSummary } from "@/services/company";

interface EmployeePickerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  employees: EmployeeSummary[];
  onSelect: (employee: EmployeeSummary) => void;
}

export function EmployeePickerDialog({
  open,
  onOpenChange,
  employees,
  onSelect,
}: EmployeePickerDialogProps) {
  const t = useT();
  const [query, setQuery] = useState("");
  const visible = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return employees
      .filter((employee) => (employee.status ?? "active") === "active")
      .filter(
        (employee) =>
          !keyword ||
          employee.name.toLowerCase().includes(keyword) ||
          employee.role_id.toLowerCase().includes(keyword),
      );
  }, [employees, query]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md" data-testid="employee-picker-dialog">
        <DialogHeader>
          <DialogTitle>{t("chat.composer.chooseEmployee")}</DialogTitle>
          <DialogDescription>{t("chat.composer.employeePicker.hint")}</DialogDescription>
        </DialogHeader>
        <div className="relative">
          <Search className="text-chat-muted pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2" />
          <Input
            value={query}
            data-testid="employee-picker-search"
            placeholder={t("chat.composer.employeePicker.search")}
            className="h-8 py-0 pl-7 text-sm"
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <div className="max-h-72 overflow-y-auto">
          {visible.length === 0 ? (
            <p className="text-chat-muted px-2 py-6 text-center text-xs">
              {t("chat.composer.employeePicker.empty")}
            </p>
          ) : (
            visible.map((employee) => (
              <button
                key={employee.employee_id}
                type="button"
                data-testid={`employee-picker-item-${employee.role_id}`}
                className="hover:bg-primary/5 flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left"
                onClick={() => {
                  onSelect(employee);
                  onOpenChange(false);
                  setQuery("");
                }}
              >
                <EmployeeAvatar
                  name={employee.name}
                  metadata={employee.metadata}
                  className="size-7 rounded-full text-xs"
                />
                <span className="min-w-0 flex-1">
                  <span className="text-chat-ink block truncate text-sm">{employee.name}</span>
                  <span className="text-chat-muted block truncate text-[11px]">
                    {employee.role_id}
                  </span>
                </span>
              </button>
            ))
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
