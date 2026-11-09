/**
 * 功能：radix-nova UI 组件导出完整性。
 * 断言：模块每个导出都可渲染（函数或 forwardRef 对象）——不假设具体名单（各组件导出面不同）。
 */
import { describe, expect, it } from "vitest";

import * as alertDialog from "@/components/ui/alert-dialog";
import * as dialog from "@/components/ui/dialog";
import * as sheet from "@/components/ui/sheet";

function expectAllRenderable(module: Record<string, unknown>): void {
  const entries = Object.entries(module);
  expect(entries.length).toBeGreaterThan(0);
  for (const [name, value] of entries) {
    const renderable = typeof value === "function" || (value !== null && typeof value === "object");
    expect(renderable, `${name} 必须可渲染（函数或 forwardRef 对象）`).toBe(true);
  }
}

describe("ui module exports", () => {
  it("alert-dialog / dialog / sheet 全部导出可渲染", () => {
    expectAllRenderable(alertDialog as unknown as Record<string, unknown>);
    expectAllRenderable(dialog as unknown as Record<string, unknown>);
    expectAllRenderable(sheet as unknown as Record<string, unknown>);
  });

  it("关键组件按名导出（forwardRef 对象）", () => {
    const alert = alertDialog as unknown as Record<string, unknown>;
    const outputs = dialog as unknown as Record<string, unknown>;
    const panel = sheet as unknown as Record<string, unknown>;
    for (const [module, names] of [
      [alert, ["AlertDialog", "AlertDialogContent", "AlertDialogOverlay", "AlertDialogAction"]],
      [outputs, ["Dialog", "DialogContent", "DialogOverlay"]],
      [panel, ["Sheet", "SheetContent"]],
    ] as const) {
      for (const name of names) {
        expect(module[name], `${name} 必须导出`).toBeDefined();
      }
    }
  });
});
