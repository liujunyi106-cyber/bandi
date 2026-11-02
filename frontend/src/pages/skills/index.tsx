/**
 * 功能：技能·连接器资源中心（技能 / SOP / MCP 连接器）。
 */
import { Plug, Plus, Search, Upload } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SegmentedTabs } from "@/components/ui/segmented-tabs";
import { useT } from "@/i18n";
import { McpConfigDialog } from "@/pages/skills/McpConfigDialog";
import { ConnectorCard, ResourceCard } from "@/pages/skills/ResourceCard";
import { ResourceDetail } from "@/pages/skills/ResourceDetail";
import { SopCreateDialog } from "@/pages/skills/SopCreateDialog";
import { SopEditor } from "@/pages/skills/SopEditor";
import { resourcesApi, type ResourceItem, type ResourceKind } from "@/services/resources";
import { settingsApi, type McpServer } from "@/services/settings";

type MarketTab = ResourceKind | "connector";
type McpMode = "intro" | "edit";

const EMPTY_RESOURCES: Record<ResourceKind, ResourceItem[]> = {
  skill: [],
  sop: [],
};

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export default function SkillsPage() {
  const t = useT();
  const [tab, setTab] = useState<MarketTab>("skill");
  const [query, setQuery] = useState("");
  const [resources, setResources] = useState<Record<ResourceKind, ResourceItem[]>>(EMPTY_RESOURCES);
  const [connectors, setConnectors] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyKey, setBusyKey] = useState("");
  const [detail, setDetail] = useState<{ kind: ResourceKind; id: string } | null>(null);
  const [editorId, setEditorId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [mcpOpen, setMcpOpen] = useState(false);
  const [mcpMode, setMcpMode] = useState<McpMode>("intro");
  const fileRef = useRef<HTMLInputElement>(null);

  const loadTab = useCallback(
    async (target: MarketTab) => {
      setLoading(true);
      setError("");
      try {
        if (target === "connector") {
          const payload = await settingsApi.mcpServers();
          setConnectors(payload.servers ?? []);
        } else {
          const payload = await resourcesApi.list(target);
          setResources((current) => ({ ...current, [target]: payload.items ?? [] }));
        }
      } catch (reason) {
        setError(errorText(reason));
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void loadTab(tab);
  }, [loadTab, tab]);

  const tabs = useMemo(
    () => [
      { value: "skill", label: t("skillsMarket.tabs.skill") },
      { value: "sop", label: t("skillsMarket.tabs.sop") },
      { value: "connector", label: t("skillsMarket.tabs.connector") },
    ],
    [t],
  );

  const keyword = query.trim().toLowerCase();
  const visibleResources = useMemo(() => {
    if (tab === "connector") return [];
    return resources[tab].filter((item) => {
      if (!keyword) return true;
      const scope = item.scope.employees.map((employee) => employee.name).join(" ");
      return [item.title, item.description, scope].some((value) =>
        value.toLowerCase().includes(keyword),
      );
    });
  }, [keyword, resources, tab]);

  const visibleConnectors = useMemo(() => {
    if (tab !== "connector") return [];
    return connectors.filter((item) => {
      if (!keyword) return true;
      return [item.name, item.command, item.url, item.category].some((value) =>
        String(value || "")
          .toLowerCase()
          .includes(keyword),
      );
    });
  }, [connectors, keyword, tab]);

  const changeTab = (value: string) => {
    setTab(value as MarketTab);
    setQuery("");
  };

  const toggleResource = async (item: ResourceItem, enabled: boolean) => {
    setBusyKey(`${item.kind}:${item.id}`);
    try {
      const result = await resourcesApi.setEnabled(item.kind, item.id, enabled);
      setResources((current) => ({
        ...current,
        [item.kind]: current[item.kind].map((entry) =>
          entry.id === item.id ? result.item : entry,
        ),
      }));
      toast.success(
        enabled
          ? t("skillsMarket.toast.enabled").replace("{name}", item.title)
          : t("skillsMarket.toast.disabled").replace("{name}", item.title),
      );
    } catch (reason) {
      toast.error(`${t("skillsMarket.toast.toggleFailed")}：${errorText(reason)}`);
    } finally {
      setBusyKey("");
    }
  };

  const toggleConnector = async (item: McpServer, enabled: boolean) => {
    setBusyKey(`mcp:${item.name}`);
    try {
      await settingsApi.patchMcpServer(item.name, { enabled });
      setConnectors((current) =>
        current.map((entry) =>
          entry.name === item.name
            ? {
                ...entry,
                enabled,
                status: enabled ? "idle" : "disabled",
              }
            : entry,
        ),
      );
      toast.success(
        enabled
          ? t("skillsMarket.toast.enabled").replace("{name}", item.name)
          : t("skillsMarket.toast.disabled").replace("{name}", item.name),
      );
    } catch (reason) {
      toast.error(`${t("skillsMarket.toast.toggleFailed")}：${errorText(reason)}`);
    } finally {
      setBusyKey("");
    }
  };

  const upload = async (file: File) => {
    if (tab === "connector") return;
    setLoading(true);
    try {
      await resourcesApi.upload(tab, file);
      toast.success(t("skillsMarket.toast.uploaded").replace("{name}", file.name));
      await loadTab(tab);
    } catch (reason) {
      toast.error(`${t("skillsMarket.toast.uploadFailed")}：${errorText(reason)}`);
      setLoading(false);
    }
  };

  const startUpload = () => {
    if (tab === "connector") {
      setMcpMode("intro");
      setMcpOpen(true);
      return;
    }
    fileRef.current?.click();
  };

  const reloadCurrent = () => {
    void loadTab(tab);
  };

  if (editorId) {
    return (
      <SopEditor
        resourceId={editorId}
        onBack={() => {
          setEditorId(null);
          reloadCurrent();
        }}
      />
    );
  }

  if (detail) {
    return (
      <ResourceDetail
        kind={detail.kind}
        resourceId={detail.id}
        onBack={() => {
          setDetail(null);
          reloadCurrent();
        }}
        onRemoved={() => {
          setDetail(null);
          reloadCurrent();
        }}
        onEdit={
          detail.kind === "sop"
            ? () => {
                setDetail(null);
                setEditorId(detail.id);
              }
            : undefined
        }
      />
    );
  }

  const isConnector = tab === "connector";
  const isEmpty = isConnector ? visibleConnectors.length === 0 : visibleResources.length === 0;

  return (
    <div className="mx-auto flex w-full max-w-[1400px] flex-col gap-6 px-8 pt-9 pb-7">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-chat-ink text-xl font-semibold">{t("skillsMarket.title")}</h1>
          <p className="text-chat-muted mt-1 text-xs">{t("skillsMarket.subtitle")}</p>
        </div>
      </header>

      <div className="flex min-h-9 flex-wrap items-center justify-between gap-3">
        <SegmentedTabs
          items={tabs}
          value={tab}
          onChange={changeTab}
          ariaLabel={t("skillsMarket.tabs.label")}
        />
        <div className="flex items-center gap-2">
          <label className="border-chat-hairline bg-card flex h-9 w-[260px] items-center gap-2 rounded-xl border px-3">
            <Search className="text-chat-muted size-4 shrink-0" aria-hidden="true" />
            <Input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t("skillsMarket.searchPlaceholder")}
              aria-label={t("skillsMarket.searchPlaceholder")}
              className="h-full min-w-0 flex-1 border-0 bg-transparent px-0 text-xs shadow-none"
            />
          </label>
          {tab === "sop" ? (
            <Button variant="outline" onClick={() => setCreateOpen(true)}>
              <Plus />
              新建 SOP
            </Button>
          ) : null}
          <Button onClick={startUpload}>
            {isConnector ? <Plug /> : <Upload />}
            {isConnector ? t("skillsMarket.customConnector") : t("skillsMarket.upload")}
          </Button>
          <input
            ref={fileRef}
            type="file"
            className="hidden"
            accept={
              tab === "sop"
                ? ".json,application/json"
                : ".md,text/markdown"
            }
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void upload(file);
              event.target.value = "";
            }}
          />
        </div>
      </div>

      {loading ? (
        <p className="text-chat-muted px-1 py-14 text-center text-xs">{t("skillsMarket.loading")}</p>
      ) : error ? (
        <div className="border-chat-hairline bg-card flex flex-col items-center gap-3 rounded-2xl border px-6 py-14 text-center">
          <p className="text-destructive text-xs">{`${t("skillsMarket.loadFailed")}：${error}`}</p>
          <Button variant="outline" onClick={reloadCurrent}>
            {t("common.retry")}
          </Button>
        </div>
      ) : isEmpty ? (
        <div className="border-chat-hairline bg-card flex min-h-[280px] flex-col items-center justify-center gap-3 rounded-2xl border px-6 text-center">
          <span className="bg-primary/5 text-chat-muted grid size-12 place-items-center rounded-2xl">
            {isConnector ? <Plug className="size-5" /> : <Upload className="size-5" />}
          </span>
          <strong className="text-chat-ink text-sm font-semibold">
            {keyword
              ? t("skillsMarket.empty.searchTitle")
              : isConnector
                ? t("skillsMarket.empty.connectorTitle")
                : t(`skillsMarket.empty.${tab}`)}
          </strong>
          <p className="text-chat-muted text-xs">
            {keyword
              ? t("skillsMarket.empty.searchHint")
              : isConnector
                ? t("skillsMarket.empty.connector")
                : t("skillsMarket.empty.hint")}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
          {isConnector
            ? visibleConnectors.map((item) => (
                <ConnectorCard
                  key={item.name}
                  item={item}
                  busy={busyKey === `mcp:${item.name}`}
                  onOpen={() => {
                    setMcpMode("edit");
                    setMcpOpen(true);
                  }}
                  onToggle={(enabled) => void toggleConnector(item, enabled)}
                />
              ))
            : visibleResources.map((item) => (
                <ResourceCard
                  key={`${item.kind}:${item.id}`}
                  item={item}
                  busy={busyKey === `${item.kind}:${item.id}`}
                  onOpen={() => setDetail({ kind: item.kind, id: item.id })}
                  onToggle={(enabled) => void toggleResource(item, enabled)}
                  onEdit={item.kind === "sop" ? () => setEditorId(item.id) : undefined}
                />
              ))}
        </div>
      )}

      <SopCreateDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={(item) => {
          void loadTab("sop");
          setEditorId(item.id);
        }}
      />

      <McpConfigDialog
        open={mcpOpen}
        initialMode={mcpMode}
        onOpenChange={setMcpOpen}
        onSaved={() => {
          setTab("connector");
          void loadTab("connector");
        }}
      />
    </div>
  );
}
