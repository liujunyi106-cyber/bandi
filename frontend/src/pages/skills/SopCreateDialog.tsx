/**
 * 功能：新建 SOP 弹窗（名称 + 标识 + 描述，创建一张可编辑的空白 SOP 卡）。
 */
import { Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { resourcesApi, type ResourceItem } from "@/services/resources";

const SKILL_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

export interface SopCreateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (item: ResourceItem) => void;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function blankCard(name: string, skillId: string, description: string): string {
  return JSON.stringify(
    {
      skill_id: skillId,
      name,
      version: "1.0.0",
      business_domain: null,
      description,
      enabled: true,
      trigger_intents: [],
      user_utterance_examples: [],
      goal: [],
      required_info: [],
      slot_filling_policy: {},
      response_rules: [],
      interruption_policy: {},
      nodes: [
        { node_id: "start", name: "开始", type: "start", instruction: "接收请求" },
        { node_id: "end", name: "结束", type: "terminal", instruction: "完成" },
      ],
      edges: [
        {
          source_node_id: "start",
          next_node_id: "end",
          condition: null,
          priority: 0,
          label: null,
        },
      ],
      start_node_id: "start",
      terminal_node_ids: ["end"],
    },
    null,
    2,
  );
}

export function SopCreateDialog({ open, onOpenChange, onCreated }: SopCreateDialogProps) {
  const [name, setName] = useState("");
  const [skillId, setSkillId] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setName("");
    setSkillId("");
    setDescription("");
    setError("");
    setBusy(false);
  }, [open]);

  const create = async () => {
    const title = name.trim();
    const id = skillId.trim();
    if (!title) {
      setError("请填写 SOP 名称");
      return;
    }
    if (!SKILL_ID_RE.test(id)) {
      setError("标识只能包含字母、数字、下划线或连字符（1-64 位）");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await resourcesApi.createSop(blankCard(title, id, description.trim()));
      toast.success("已创建 SOP");
      onCreated(result.item);
      onOpenChange(false);
    } catch (reason) {
      setError(errorText(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[460px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-base">
            <span className="bg-primary text-primary-foreground grid size-8 place-items-center rounded-[10px]">
              <Plus className="size-4" />
            </span>
            新建 SOP
          </DialogTitle>
          <DialogDescription>
            创建一张空白 SOP 卡（开始 → 结束），随后进入可视化编辑器补充流程。
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <label className="block">
            <span className="text-chat-muted mb-1 block text-[11px]">SOP 名称</span>
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="如：新品上架 SOP"
              aria-label="SOP 名称"
            />
          </label>
          <label className="block">
            <span className="text-chat-muted mb-1 block text-[11px]">标识（skill_id）</span>
            <Input
              value={skillId}
              onChange={(event) => setSkillId(event.target.value)}
              placeholder="如：new-product-launch"
              aria-label="SOP 标识"
            />
            <span className="text-muted-foreground-weak mt-1 block text-[10px]">
              创建后不可修改，用于文件名与员工绑定。
            </span>
          </label>
          <label className="block">
            <span className="text-chat-muted mb-1 block text-[11px]">描述（可选）</span>
            <Input
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="一句话说明这条 SOP 的用途"
              aria-label="SOP 描述"
            />
          </label>
          {error ? <p className="text-destructive text-[11px]">{error}</p> : null}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            取消
          </Button>
          <Button onClick={() => void create()} disabled={busy}>
            {busy ? "创建中…" : "创建并编辑"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
