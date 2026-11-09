/**
 * 功能：把共享 WS 单例接到单页壳生命周期。
 * 整个 App 只在壳层连一次；状态镜像进 ui store 供顶栏指示。
 */
import { useEffect } from "react";

import { socket } from "@/services/ws";
import { useUiStore } from "@/stores/ui-store";

export function useKernelSocket(): void {
  const setWsStatus = useUiStore((state) => state.setWsStatus);

  useEffect(() => {
    const unsubscribe = socket.onStatus(setWsStatus);
    setWsStatus(socket.getStatus());
    socket.connect();
    return unsubscribe;
  }, [setWsStatus]);
}
