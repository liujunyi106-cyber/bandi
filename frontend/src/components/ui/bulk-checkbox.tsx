/**
 * 功能：批量选择复选框——轻圆角（4px）+ 边缘线 = 多选卡片底色
 * （border-primary/5，与「边缘线统一清单」同源）；选中态实心主色 + 白勾。
 * 保留原生 input[type=checkbox] 语义（checked/onChange、测试与可访问性不变）。
 */
import { cn } from "@/lib/utils";

export function BulkCheckbox({
  checked,
  onChange,
  testId,
  className,
  stopPropagation,
}: {
  checked: boolean;
  onChange: () => void;
  testId?: string;
  className?: string;
  /** 功能：阻止点击冒泡（复选框嵌在可点击行按钮内时用——防父级 onClick 二次切换）。 */
  stopPropagation?: boolean;
}) {
  return (
    <input
      type="checkbox"
      data-testid={testId}
      checked={checked}
      onChange={onChange}
      onClick={stopPropagation ? (event) => event.stopPropagation() : undefined}
      className={cn("bulk-checkbox", className)}
    />
  );
}
