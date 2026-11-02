/**
 * 功能：员工列表加载（群聊/单聊/ChatView 空态共用）。
 */
import { useEffect, useState } from "react";

import { companyApi, type EmployeeSummary } from "@/services/company";

export interface EmployeesState {
  employees: EmployeeSummary[];
  /** 成功加载完成（失败保持 false——空态引导只在确认无员工时展示）。 */
  loaded: boolean;
}

export function useEmployees(): EmployeesState {
  const [state, setState] = useState<EmployeesState>({ employees: [], loaded: false });

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      void companyApi
        .employees()
        .then((result) => {
          if (!cancelled) setState({ employees: result.employees, loaded: true });
        })
        .catch(() => undefined);
    };
    load();
    window.addEventListener("bandi:employees-changed", load);
    window.addEventListener("focus", load);
    return () => {
      cancelled = true;
      window.removeEventListener("bandi:employees-changed", load);
      window.removeEventListener("focus", load);
    };
  }, []);

  return state;
}
