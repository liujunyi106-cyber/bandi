/**
 * 功能：Day135 脚手架期的页面占位符——把每个导航入口的归属日与形态写清楚
 * 后续 Day 逐页替换为真实实现（@base docs/前端设计规划.md §6 功能分区矩阵）。
 */
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useT } from "@/i18n";

interface PagePlaceholderProps {
  /** i18n 标题 key，如 `nav.team`。 */
  titleKey: string;
  /** 规划归属日，如 `Day140`。 */
  day: string;
  /** 该页形态一句话说明。 */
  note: string;
}

export function PagePlaceholder({ titleKey, day, note }: PagePlaceholderProps) {
  const t = useT();
  return (
    <div className="flex h-full items-center justify-center p-8">
      <Card className="w-full max-w-lg shadow-soft">
        <CardHeader>
          <CardTitle className="text-base">{t(titleKey)}</CardTitle>
          <CardDescription>{day} · 待实施</CardDescription>
        </CardHeader>
        <CardContent className="text-muted-foreground text-sm leading-relaxed">{note}</CardContent>
      </Card>
    </div>
  );
}
