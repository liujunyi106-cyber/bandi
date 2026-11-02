/**
 * 功能：组织编排视图（ReactFlow 组织树画布 + 角色检查器 + 未分配员工区）。
 * 节点为角色，边为汇报关系；拖线设置上级，删除连线取消汇报。保存端点接入前为本地预览。
 */
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  addEdge,
  useEdgesState,
  useNodesState,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import dagre from "dagre";
import { UsersRound } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import "@xyflow/react/dist/style.css";

import { Button } from "@/components/ui/button";
import { TeamAvatar } from "@/components/team/TeamAvatar";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import type { EmployeeSummary, OrgTeamSummary } from "@/services/company";
import { describeError, teamApi, type OrgRoleSummary } from "@/services/team";

const NODE_WIDTH = 188;
const NODE_HEIGHT = 56;

export interface RoleNodeData extends Record<string, unknown> {
  name: string;
  roleId: string;
  teamName: string;
  memberCount: number;
  finalDecider: boolean;
  members: Array<{ name: string; metadata?: Record<string, unknown> | null }>;
}

export interface OrgChartViewProps {
  roles: OrgRoleSummary[];
  orgTeams: OrgTeamSummary[];
  employees: EmployeeSummary[];
  selectedTeamId?: string;
  onChanged: () => void;
}

/** 功能：角色节点主体（头像 + 名称 + 团队/人数，可独立测试）。 */
export function RoleNodeBody({ data }: { data: RoleNodeData }) {
  return (
    <div className="flex items-center gap-2">
      <TeamAvatar members={data.members} name={data.name} fallback="initial" className="size-6" />
      <div className="min-w-0">
        <p className="text-chat-ink truncate text-[11px] font-semibold">{data.name}</p>
        <p className="text-chat-muted truncate text-[9px]">
          {data.teamName}
          {data.memberCount > 0 ? ` · ${data.memberCount}` : ""}
        </p>
      </div>
    </div>
  );
}

/** 功能：角色画布节点（可独立测试的纯展示层）。 */
export function RoleNodeCard({ data, selected }: { data: RoleNodeData; selected?: boolean }) {
  return (
    <div
      className={cn(
        "group bg-card border-chat-hairline relative h-14 w-[188px] rounded-xl border px-3 py-2 shadow-sm transition-colors",
        selected && "border-primary",
      )}
    >
      <Handle
        type="target"
        position={Position.Bottom}
        style={{ bottom: 0 }}
        className="size-1.5! border-0! bg-primary/40! opacity-0! transition-opacity group-hover:opacity-100!"
      />
      <RoleNodeBody data={data} />
      <Handle
        type="source"
        position={Position.Top}
        style={{ top: 0 }}
        className="size-1.5! border-0! bg-primary/40! opacity-0! transition-opacity group-hover:opacity-100!"
      />
    </div>
  );
}

/** 功能：角色画布节点。 */
function RoleNode({ data, selected }: NodeProps<Node<RoleNodeData>>) {
  return <RoleNodeCard data={data} selected={selected} />;
}

const NODE_TYPES = { role: RoleNode };

/** 功能：组织画布与角色检查器。 */
export function OrgChartView({
  roles,
  orgTeams,
  employees,
  selectedTeamId = "",
  onChanged,
}: OrgChartViewProps) {
  const t = useT();
  const [selectedRoleId, setSelectedRoleId] = useState("");
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<RoleNodeData>>([]);
  const [edges, setEdges] = useEdgesState<Edge>([]);
  const storageKey = `bandi:org-positions:${selectedTeamId || "all"}`;
  const [savedPositions, setSavedPositions] = useState<Record<string, { x: number; y: number }>>(
    {},
  );

  const teamNameById = useMemo(() => {
    const map = new Map<string, string>();
    orgTeams.forEach((team) => {
      team.roles.forEach((role) => map.set(role.role_id, team.name));
    });
    return map;
  }, [orgTeams]);

  const teamIdByRoleId = useMemo(() => {
    const map = new Map<string, string>();
    orgTeams.forEach((team) => {
      team.roles.forEach((role) => map.set(role.role_id, team.team_id));
    });
    return map;
  }, [orgTeams]);

  const memberCount = useMemo(() => {
    const map = new Map<string, number>();
    employees.forEach((employee) => {
      if ((employee.status ?? "active") !== "active") return;
      map.set(employee.role_id, (map.get(employee.role_id) ?? 0) + 1);
    });
    return map;
  }, [employees]);

  /** 功能：角色成员头像（按 role_id 最多取 4 个在职员工，节点头像与员工画廊同源）。 */
  const membersByRole = useMemo(() => {
    const map = new Map<
      string,
      Array<{ name: string; metadata?: Record<string, unknown> | null }>
    >();
    employees.forEach((employee) => {
      if ((employee.status ?? "active") !== "active") return;
      const list = map.get(employee.role_id) ?? [];
      if (list.length >= 4) return;
      list.push({ name: employee.name, metadata: employee.metadata });
      map.set(employee.role_id, list);
    });
    return map;
  }, [employees]);

  const roleIds = useMemo(() => new Set(roles.map((role) => role.id)), [roles]);

  const visibleRoles = useMemo(
    () =>
      roles.filter(
        (role) =>
          teamNameById.has(role.id) &&
          (!selectedTeamId || teamIdByRoleId.get(role.id) === selectedTeamId),
      ),
    [roles, selectedTeamId, teamIdByRoleId, teamNameById],
  );

  const unassignedRoles = useMemo(
    () => roles.filter((role) => !teamNameById.has(role.id) && (memberCount.get(role.id) ?? 0) > 0),
    [memberCount, roles, teamNameById],
  );

  const layoutNodes = useMemo<Node<RoleNodeData>[]>(() => {
    const graph = new dagre.graphlib.Graph();
    graph.setDefaultEdgeLabel(() => ({}));
    graph.setGraph({ rankdir: "TB", nodesep: 28, ranksep: 56 });
    visibleRoles.forEach((role) => {
      graph.setNode(role.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
    });
    visibleRoles.forEach((role) => {
      if (role.reports_to && visibleRoles.some((item) => item.id === role.reports_to)) {
        graph.setEdge(role.reports_to, role.id);
      }
    });
    dagre.layout(graph);
    return visibleRoles.map((role) => {
      const point = graph.node(role.id) as { x: number; y: number } | undefined;
      return {
        id: role.id,
        type: "role",
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        position: savedPositions[role.id] ?? {
          x: (point?.x ?? 0) - NODE_WIDTH / 2,
          y: (point?.y ?? 0) - NODE_HEIGHT / 2,
        },
        data: {
          name: role.name,
          roleId: role.id,
          teamName: teamNameById.get(role.id) ?? t("team.orgChart.unassigned"),
          memberCount: memberCount.get(role.id) ?? 0,
          finalDecider: role.is_final_decider,
          members: membersByRole.get(role.id) ?? [],
        },
      };
    });
  }, [memberCount, membersByRole, savedPositions, t, teamNameById, visibleRoles]);

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(storageKey);
      setSavedPositions(raw ? (JSON.parse(raw) as Record<string, { x: number; y: number }>) : {});
    } catch {
      setSavedPositions({});
    }
  }, [storageKey]);

  useEffect(() => {
    setNodes(layoutNodes);
  }, [layoutNodes, setNodes]);

  const savePositions = useCallback(() => {
    const positions = Object.fromEntries(nodes.map((node) => [node.id, node.position]));
    setSavedPositions(positions);
    try {
      window.localStorage.setItem(storageKey, JSON.stringify(positions));
    } catch {
      // 功能：本地存储不可用时仅保留内存位置。
    }
  }, [nodes, storageKey]);

  const removeFromTeam = async () => {
    if (!selectedRoleId || !selectedTeamId) return;
    try {
      await teamApi.updateRole(selectedRoleId, { team_id: "" });
      toast.success(t("team.orgChart.removedFromTeam"));
      setSelectedRoleId("");
      onChanged();
    } catch (error) {
      toast.error(describeError(error, t("team.orgChart.edgeFailed")));
    }
  };

  const clearManager = async () => {
    if (!selectedRoleId) return;
    try {
      await teamApi.updateRole(selectedRoleId, { reports_to: null });
      toast.success(t("team.orgChart.edgeSaved"));
      onChanged();
    } catch (error) {
      toast.error(describeError(error, t("team.orgChart.edgeFailed")));
    }
  };

  const initialEdges = useMemo<Edge[]>(
    () =>
      visibleRoles
        .filter(
          (role) => role.reports_to && visibleRoles.some((item) => item.id === role.reports_to),
        )
        .map((role) => ({
          id: `${role.id}->${role.reports_to}`,
          source: role.id,
          target: role.reports_to as string,
          type: "smoothstep",
          markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
          style: { stroke: "#c7ccd8", strokeWidth: 1.4 },
        })),
    [visibleRoles],
  );

  useEffect(() => {
    setEdges(initialEdges);
  }, [initialEdges, setEdges]);

  useEffect(() => {
    if (selectedRoleId && !roles.some((role) => role.id === selectedRoleId)) {
      setSelectedRoleId("");
    }
  }, [roles, selectedRoleId]);

  const selectedRole = roles.find((role) => role.id === selectedRoleId) ?? null;
  const members = selectedRole
    ? employees.filter((employee) => employee.role_id === selectedRole.id)
    : [];
  const subordinates = selectedRole
    ? roles.filter((role) => role.reports_to === selectedRole.id)
    : [];
  const manager = selectedRole?.reports_to
    ? (roles.find((role) => role.id === selectedRole.reports_to) ?? null)
    : null;
  const orphanEmployees = useMemo(
    () => employees.filter((employee) => !roleIds.has(employee.role_id)),
    [employees, roleIds],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      if (!connection.source || !connection.target || connection.source === connection.target) {
        return;
      }
      const edgeId = `${connection.source}->${connection.target}`;
      const childId = connection.source;
      const parentId = connection.target;
      setEdges((current) =>
        addEdge(
          {
            ...connection,
            id: edgeId,
            type: "smoothstep",
            markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
            style: { stroke: "#c7ccd8", strokeWidth: 1.4 },
          },
          current.filter((edge) => edge.source !== childId),
        ),
      );
      teamApi
        .updateRole(childId, { reports_to: parentId })
        .then(() => {
          toast.success(t("team.orgChart.edgeSaved"));
          onChanged();
        })
        .catch((error) => {
          setEdges((current) => current.filter((edge) => edge.id !== edgeId));
          toast.error(describeError(error, t("team.orgChart.edgeFailed")));
        });
    },
    [onChanged, setEdges, t],
  );

  const onEdgesDelete = useCallback(
    (removed: Edge[]) => {
      setEdges((current) => current.filter((edge) => !removed.some((item) => item.id === edge.id)));
      void Promise.allSettled(
        removed.map((edge) => teamApi.updateRole(edge.source, { reports_to: null })),
      ).then((results) => {
        if (results.some((result) => result.status === "rejected")) {
          toast.error(t("team.orgChart.edgeFailed"));
        } else {
          toast.success(t("team.orgChart.edgeSaved"));
        }
        onChanged();
      });
    },
    [onChanged, setEdges, t],
  );

  if (visibleRoles.length === 0 && unassignedRoles.length === 0 && orphanEmployees.length === 0) {
    return (
      <div className="border-chat-hairline text-chat-muted rounded-2xl border border-dashed px-6 py-14 text-center text-xs">
        {t("team.orgChart.empty")}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="relative">
        <div className="border-chat-hairline bg-primary/[0.01] h-[600px] overflow-hidden rounded-2xl border">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={NODE_TYPES}
            onNodesChange={onNodesChange}
            onConnect={onConnect}
            onEdgesDelete={onEdgesDelete}
            onNodeDragStop={savePositions}
            onNodeClick={(_, node) => setSelectedRoleId(node.id)}
            nodesConnectable
            edgesFocusable
            deleteKeyCode={["Backspace", "Delete"]}
            fitView
            fitViewOptions={{ padding: 0.12 }}
            minZoom={0.4}
            maxZoom={1.4}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={18} size={1} color="#eef0f4" />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>

        {selectedRole ? (
          <aside className="bg-card border-chat-hairline mt-3 h-fit rounded-2xl border p-4 lg:absolute lg:top-3 lg:right-3 lg:z-10 lg:mt-0 lg:max-h-[calc(600px-24px)] lg:w-[280px] lg:overflow-y-auto lg:shadow-sm">
            <div className="flex flex-col gap-3">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="text-chat-muted text-[10px]">{t("team.orgChart.identity")}</p>
                  <h3 className="text-chat-ink mt-1 truncate text-sm font-semibold">
                    {selectedRole.name}
                  </h3>
                  <p className="text-chat-muted mt-0.5 text-[11px]">
                    {selectedRole.id} ·{" "}
                    {teamNameById.get(selectedRole.id) ?? t("team.orgChart.unassigned")}
                  </p>
                </div>
                <button
                  type="button"
                  aria-label={t("team.orgChart.close")}
                  onClick={() => setSelectedRoleId("")}
                  className="text-chat-muted hover:text-chat-ink shrink-0 text-xs"
                >
                  ✕
                </button>
              </div>
              <div>
                <p className="text-chat-muted text-[10px]">{t("team.orgChart.responsibility")}</p>
                <p className="text-chat-ink mt-1 text-xs leading-5">
                  {selectedRole.responsibility || t("team.orgChart.noResponsibility")}
                </p>
              </div>
              <div className="border-chat-hairline grid grid-cols-2 gap-2 border-t pt-3">
                <DetailField
                  label={t("team.orgChart.reportsTo")}
                  value={manager?.name ?? t("team.orgChart.topLevel")}
                />
                <DetailField
                  label={t("team.orgChart.subordinates")}
                  value={`${subordinates.length}`}
                />
                <DetailField label={t("team.orgChart.members")} value={`${members.length}`} />
                <DetailField
                  label={t("team.orgChart.finalDecider")}
                  value={
                    selectedRole.is_final_decider ? t("team.orgChart.yes") : t("team.orgChart.no")
                  }
                />
              </div>
              <div className="flex flex-wrap gap-2">
                {manager ? (
                  <Button variant="outline" size="sm" onClick={() => void clearManager()}>
                    {t("team.orgChart.clearManager")}
                  </Button>
                ) : null}
                {selectedTeamId ? (
                  <Button size="sm" onClick={() => void removeFromTeam()}>
                    {t("team.orgChart.removeFromTeam")}
                  </Button>
                ) : null}
              </div>
              {selectedRole.tool_tags.length > 0 ? (
                <div>
                  <p className="text-chat-muted text-[10px]">{t("team.orgChart.tools")}</p>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {selectedRole.tool_tags.map((tag) => (
                      <span
                        key={tag}
                        className="bg-primary/[0.03] text-chat-muted rounded px-1.5 py-0.5 text-[10px]"
                      >
                        {tag}
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}
              {members.length === 0 ? (
                <p className="text-chat-muted text-[11px]">{t("team.orgChart.noMembers")}</p>
              ) : null}
            </div>
          </aside>
        ) : null}
      </div>

      {unassignedRoles.length > 0 || orphanEmployees.length > 0 ? (
        <section className="bg-card border-chat-hairline rounded-2xl border p-4">
          <div className="flex items-center gap-2">
            <UsersRound className="text-chat-muted size-4" aria-hidden="true" />
            <h3 className="text-chat-ink text-xs font-semibold">
              {t("team.orgChart.unassignedTitle")} (
              {unassignedRoles.length + orphanEmployees.length})
            </h3>
          </div>
          <p className="text-chat-muted mt-1 text-[11px]">{t("team.orgChart.unassignedHint")}</p>
          {unassignedRoles.length > 0 ? (
            <div className="mt-2">
              <p className="text-chat-muted text-[10px]">{t("team.orgChart.unassignedRoles")}</p>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {unassignedRoles.map((role) => (
                  <button
                    key={role.id}
                    type="button"
                    onClick={() => setSelectedRoleId(role.id)}
                    className={cn(
                      "bg-primary/[0.03] text-chat-muted hover:text-chat-ink rounded px-2 py-1 text-[11px]",
                      selectedRoleId === role.id && "ring-1 ring-primary",
                    )}
                  >
                    {role.name} · {memberCount.get(role.id) ?? 0}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
          {orphanEmployees.length > 0 ? (
            <div className="mt-2">
              <p className="text-chat-muted text-[10px]">{t("team.orgChart.orphans")}</p>
              <div className="mt-1 flex flex-wrap gap-1.5">
                {orphanEmployees.map((employee) => (
                  <span
                    key={employee.employee_id}
                    className="bg-primary/[0.03] text-chat-muted rounded px-2 py-1 text-[11px]"
                  >
                    {employee.name} · {employee.role_id}
                  </span>
                ))}
              </div>
            </div>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-chat-muted text-[10px]">{label}</p>
      <p className="text-chat-ink mt-0.5 truncate text-[11px]">{value}</p>
    </div>
  );
}
