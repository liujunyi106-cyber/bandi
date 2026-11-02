/**
 * 功能：实时窗口宽度（面板自适应尺寸用；SSR/测试环境回落 1024 = 桌面最小窗口）。
 */
import { useEffect, useState } from "react";

export const WINDOW_WIDTH_FALLBACK = 1024;

export function useWindowWidth(): number {
  const [width, setWidth] = useState(() =>
    typeof window === "undefined" ? WINDOW_WIDTH_FALLBACK : window.innerWidth,
  );
  useEffect(() => {
    const onResize = () => setWidth(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return width;
}
