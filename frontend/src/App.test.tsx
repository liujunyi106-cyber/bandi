import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

vi.mock("sonner", () => ({
  Toaster: ({ position }: { position?: string }) => (
    <div data-testid="global-toaster" data-position={position} />
  ),
}));
vi.mock("@/components/layout/AppShell", () => ({ AppShell: () => <div /> }));
vi.mock("@/pages/automation", () => ({ default: () => null }));
vi.mock("@/pages/chat", () => ({ default: () => null }));
vi.mock("@/pages/knowledge", () => ({ default: () => null }));
vi.mock("@/pages/settings", () => ({ default: () => null }));
vi.mock("@/pages/shared", () => ({ default: () => null }));
vi.mock("@/pages/skills", () => ({ default: () => null }));
vi.mock("@/pages/team", () => ({ default: () => null }));
vi.mock("@/pages/work", () => ({ default: () => null }));

import App from "./App";

describe("App 全局 Toast", () => {
  it("顶部居中以避开右上操作按钮与底部输入区", () => {
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("global-toaster")).toHaveAttribute("data-position", "top-center");
  });
});
