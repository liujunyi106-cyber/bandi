/**
  * 功能：shadcn/ui Sidebar（单页壳一体左栏；）。
 * 宽度：展开宽度随窗口自适应（216@1920 → 184@1024 → 152@640，窄窗继续缩窄不折叠）
  * 手动折叠 64px icon rail；桌面产品不做移动端适配——**任何窗口尺寸下左栏都常驻**
 * 不退化为滑出抽屉（修复：原 980px 断点会让左栏整体消失）。
 */
import { Slot } from "@radix-ui/react-slot";
import * as Separator from "@radix-ui/react-separator";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import * as React from "react";

import { PanelToggleIcon } from "@/components/layout/PanelToggleIcon";
import { cn } from "@/lib/utils";

const SIDEBAR_WIDTH = "216px";
const SIDEBAR_WIDTH_ICON = "64px";
const SIDEBAR_KEYBOARD_SHORTCUT = "b";
const STORAGE_KEY = "bandi:sidebar-open";

type SidebarContextValue = {
  state: "expanded" | "collapsed";
  open: boolean;
  setOpen: (open: boolean) => void;
  toggleSidebar: () => void;
};

const SidebarContext = React.createContext<SidebarContextValue | null>(null);

/** 功能：读取侧栏上下文（必须在 SidebarProvider 内使用）。 */
export function useSidebar(): SidebarContextValue {
  const context = React.useContext(SidebarContext);
  if (!context) throw new Error("useSidebar 必须在 <SidebarProvider> 内使用");
  return context;
}

export interface SidebarProviderProps extends React.ComponentProps<"div"> {
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

/** 功能：侧栏状态容器（持久化展开态 + Cmd/Ctrl+B 快捷键）。 */
export function SidebarProvider({
  defaultOpen = true,
  open: openProp,
  onOpenChange,
  className,
  children,
  style,
  ...props
}: SidebarProviderProps) {
  const [internalOpen, setInternalOpen] = React.useState<boolean>(() => {
    if (typeof window === "undefined") return defaultOpen;
    const saved = window.localStorage.getItem(STORAGE_KEY);
    return saved === null ? defaultOpen : saved === "true";
  });

  const open = openProp ?? internalOpen;

  const setOpen = React.useCallback(
    (next: boolean) => {
      onOpenChange?.(next);
      if (openProp === undefined) {
        setInternalOpen(next);
        if (typeof window !== "undefined") window.localStorage.setItem(STORAGE_KEY, String(next));
      }
    },
    [onOpenChange, openProp],
  );

  const toggleSidebar = React.useCallback(() => setOpen(!open), [setOpen, open]);

  React.useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === SIDEBAR_KEYBOARD_SHORTCUT && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        toggleSidebar();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [toggleSidebar]);

  const state: "expanded" | "collapsed" = open ? "expanded" : "collapsed";

  return (
    <SidebarContext.Provider value={{ state, open, setOpen, toggleSidebar }}>
      <TooltipPrimitive.Provider delayDuration={200}>
        <div
          data-slot="sidebar-wrapper"
          style={
            {
              "--sidebar-width": SIDEBAR_WIDTH,
              "--sidebar-width-icon": SIDEBAR_WIDTH_ICON,
              ...style,
            } as React.CSSProperties
          }
          className={cn(
            "group/sidebar-wrapper flex h-[var(--app-h,100svh)] w-full overflow-hidden",
            className,
          )}
          {...props}
        >
          {children}
        </div>
      </TooltipPrimitive.Provider>
    </SidebarContext.Provider>
  );
}

export interface SidebarProps extends React.ComponentProps<"div"> {
  side?: "left" | "right";
  variant?: "sidebar" | "floating" | "inset";
  collapsible?: "offcanvas" | "icon" | "none";
}

/** 功能：一体左栏容器（展开宽度自适应 / 折叠 64px icon rail；任何窗口尺寸都常驻）。 */
export function Sidebar({
  side = "left",
  variant = "sidebar",
  collapsible = "offcanvas",
  className,
  children,
  ...props
}: SidebarProps) {
  const { state } = useSidebar();

  if (collapsible === "none") {
    return (
      <div
        data-slot="sidebar"
        className={cn(
          // 功能：常驻左栏跟随外壳高度（--app-h 已补偿字号缩放），通知/设置贴底
          "bg-sidebar text-sidebar-foreground flex h-full w-(--sidebar-width) shrink-0 flex-col",
          className,
        )}
        {...props}
      >
        {children}
      </div>
    );
  }

  return (
    <div
      className="group peer text-sidebar-foreground shrink-0"
      data-state={state}
      data-collapsible={state === "collapsed" ? collapsible : ""}
      data-variant={variant}
      data-side={side}
      data-slot="sidebar"
    >
      {/* 占位宽度（内容随状态变化，避免重排抖动） */}
      <div
        data-slot="sidebar-gap"
        className={cn(
          "relative w-(--sidebar-width) bg-transparent transition-[width] duration-200 ease-linear",
          "group-data-[collapsible=offcanvas]:w-0",
          "group-data-[collapsible=icon]:w-(--sidebar-width-icon)",
        )}
      />
      <div
        data-slot="sidebar-container"
        className={cn(
          "fixed inset-y-0 z-10 flex h-[var(--app-h,100svh)] w-(--sidebar-width) transition-[left,right,width] duration-200 ease-linear",
          side === "left"
            ? "left-0 group-data-[collapsible=offcanvas]:left-[calc(var(--sidebar-width)*-1)]"
            : "right-0 group-data-[collapsible=offcanvas]:right-[calc(var(--sidebar-width)*-1)]",
          "group-data-[collapsible=icon]:w-(--sidebar-width-icon)",
          className,
        )}
        {...props}
      >
        <div
          data-sidebar="sidebar"
          className={cn(
            "bg-sidebar flex h-full w-full flex-col border-r border-sidebar-border",
            "group-data-[variant=floating]:shadow-soft group-data-[variant=floating]:rounded-lg group-data-[variant=floating]:border",
          )}
        >
          {children}
        </div>
      </div>
    </div>
  );
}

/** 功能：顶栏折叠开关。 */
export function SidebarTrigger({ className, onClick, ...props }: React.ComponentProps<"button">) {
  const { toggleSidebar } = useSidebar();
  return (
    <button
      data-slot="sidebar-trigger"
      type="button"
      aria-label="切换侧栏"
      className={cn(
        "text-muted-foreground hover:bg-primary/5 hover:text-accent-foreground inline-flex size-8 items-center justify-center rounded-[8px]",
        className,
      )}
      onClick={(event) => {
        onClick?.(event);
        toggleSidebar();
      }}
      {...props}
    >
      <PanelToggleIcon side="left" />
    </button>
  );
}

/** 功能：侧栏收起/展开的拖拽轨（点击切换）。 */
export function SidebarRail({ className, ...props }: React.ComponentProps<"button">) {
  const { toggleSidebar } = useSidebar();
  return (
    <button
      data-slot="sidebar-rail"
      type="button"
      aria-label="切换侧栏"
      onClick={toggleSidebar}
      className={cn(
        "hover:after:bg-sidebar-border absolute inset-y-0 z-20 flex w-3 -translate-x-1/2 transition-all ease-linear group-data-[side=left]:-right-3 group-data-[side=right]:left-0 after:absolute after:inset-y-0 after:left-1/2 after:w-[2px]",
        className,
      )}
      {...props}
    />
  );
}

/** 功能：侧栏右侧的主内容容器（顶栏 + 内容区）。 */
export function SidebarInset({ className, ...props }: React.ComponentProps<"main">) {
  return (
    <main
      data-slot="sidebar-inset"
      className={cn(
        "bg-background relative flex h-full min-h-0 w-full flex-1 flex-col overflow-hidden",
        className,
      )}
      {...props}
    />
  );
}

/** 功能：侧栏顶部区（Logo / 新建任务）。 */
export function SidebarHeader({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-header"
      className={cn("flex flex-col gap-2 p-2", className)}
      {...props}
    />
  );
}

/** 功能：侧栏底部区（设置）。 */
export function SidebarFooter({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-footer"
      className={cn("flex flex-col gap-2 p-2", className)}
      {...props}
    />
  );
}

/** 功能：侧栏可滚动主体。 */
export function SidebarContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-content"
      className={cn(
        "flex min-h-0 flex-1 flex-col gap-2 overflow-auto group-data-[collapsible=icon]:overflow-hidden",
        className,
      )}
      {...props}
    />
  );
}

/** 功能：侧栏分组。 */
export function SidebarGroup({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-group"
      className={cn("relative flex w-full min-w-0 flex-col p-2", className)}
      {...props}
    />
  );
}

/** 功能：分组标题（折叠态隐藏）。 */
export function SidebarGroupLabel({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      data-slot="sidebar-group-label"
      className={cn(
        "text-muted-foreground flex h-8 shrink-0 items-center rounded-md px-2 text-xs font-medium group-data-[collapsible=icon]:hidden",
        className,
      )}
      {...props}
    />
  );
}

/** 功能：分组内容容器。 */
export function SidebarGroupContent({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div data-slot="sidebar-group-content" className={cn("w-full text-sm", className)} {...props} />
  );
}

/** 功能：侧栏分隔线。 */
export function SidebarSeparator({
  className,
  ...props
}: React.ComponentProps<typeof Separator.Root>) {
  return (
    <Separator.Root
      data-slot="sidebar-separator"
      className={cn("bg-sidebar-border mx-2 h-px w-auto shrink-0", className)}
      {...props}
    />
  );
}

/** 功能：菜单列表容器。 */
export function SidebarMenu({ className, ...props }: React.ComponentProps<"ul">) {
  return (
    <ul
      data-slot="sidebar-menu"
      className={cn("flex w-full min-w-0 flex-col gap-1", className)}
      {...props}
    />
  );
}

/** 功能：菜单项。 */
export function SidebarMenuItem({ className, ...props }: React.ComponentProps<"li">) {
  return (
    <li
      data-slot="sidebar-menu-item"
      className={cn("group/menu-item relative", className)}
      {...props}
    />
  );
}

const sidebarMenuButtonVariants =
  "peer/menu-button text-sidebar-foreground hover:bg-primary/5 hover:text-sidebar-accent-foreground flex w-full items-center gap-2 overflow-hidden rounded-lg px-2 py-1.5 text-left text-[13px] outline-none transition-[width,height,padding] focus-visible:ring-2 focus-visible:ring-sidebar-ring disabled:pointer-events-none disabled:opacity-50 group-data-[collapsible=icon]:size-9 group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:p-0 [&>span:last-child]:truncate [&>svg]:size-4 [&>svg]:shrink-0";

export interface SidebarMenuButtonProps extends React.ComponentProps<"button"> {
  asChild?: boolean;
  isActive?: boolean;
  tooltip?: string;
}

/** 功能：菜单按钮（折叠态只显图标；tooltip 折叠态可见）。 */
export function SidebarMenuButton({
  asChild = false,
  isActive = false,
  tooltip,
  className,
  ...props
}: SidebarMenuButtonProps) {
  const { state } = useSidebar();
  const Comp = asChild ? Slot : "button";

  const button = (
    <Comp
      data-slot="sidebar-menu-button"
      data-active={isActive}
      className={cn(
        sidebarMenuButtonVariants,
        isActive && "bg-sidebar-accent text-sidebar-accent-foreground font-medium",
        className,
      )}
      {...props}
    />
  );

  if (!tooltip || state !== "collapsed") return button;

  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{button}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side="right"
          align="center"
          sideOffset={6}
          className="bg-foreground text-background z-50 rounded-md px-2 py-1 text-xs shadow-float"
        >
          {tooltip}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}
