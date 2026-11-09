/**
 * 功能：Composer 控制条——组合 [思考滑杆][模型字段] …… [发送/停止]。
 * 思考 = 滑杆字段（左侧）；模型 = 两态字段 + 底部配置入口 → 配置弹窗（档案级，全局生效）。
 * 会话级 model/thinking 覆盖仍走 -2 的 payload 通道（与档案级共存）。
 */
import { useState } from "react";

import { ModelConfigDialog } from "@/components/chat/ModelConfigDialog";
import { ModelField } from "@/components/chat/ModelField";
import { ContextUsageRing } from "@/components/chat/ContextUsageRing";
import { ThinkingSlider } from "@/components/chat/ThinkingSlider";
import { normalizeModelKey } from "@/lib/model-key";
import type { ModelOption, ProfileSummary } from "@/services/settings";

interface ComposerControlsProps {
  /** 展示模型（全局激活档案的模型；员工独立模型时由父层传员工模型）。 */
  model: string;
  options: ModelOption[];
  thinking: string;
  /** 激活档案摘要（null = 未配置档案，跟随 config.yaml）。 */
  activeProfile: ProfileSummary | null;
  /** 已配置档案—— A 方案：点候选 = **激活该档案**（全局切换）。 */
  profiles?: ProfileSummary[];
  /** 员工独立模型（非空 = 该员工用自己的模型，锁定显示且禁止全局切换）。 */
  employeeModel?: string;
  /** 功能：切换全局档案（A 方案：控制条切换 = 全局生效）。 */
  onActivateProfile?: (profileId: string) => void;
  onThinking: (mode: string) => void;
  /** 档案保存/激活/删除后回调（父层刷新模型数据源）。 */
  onProfilesChanged: () => void;
  /** 递增信号：外部（model 斜杠命令）请求展开模型下拉。 */
  openSignal?: number;
  /** 当前会话 id（上下文用量环的数据源；空串 = 不请求）。 */
  sessionId?: string;
  /** 递增信号：一轮结束（busy 由真变假）时重取上下文用量。 */
  usageRefreshKey?: number;
}

export function ComposerControls({
  model,
  options,
  thinking,
  activeProfile,
  profiles = [],
  employeeModel = "",
  onActivateProfile,
  onThinking,
  onProfilesChanged,
  openSignal = 0,
  sessionId = "",
  usageRefreshKey = 0,
}: ComposerControlsProps) {
  const [configOpen, setConfigOpen] = useState(false);
  // 第八轮：底部「配置模型」= **新建**（与设置页「新建」一致）；
  // 点击当前生效行 = 编辑该档案（configTarget 切换两种语义）
  const [configTarget, setConfigTarget] = useState<ProfileSummary | null>(null);

  // 当前模型的思考挡位：档案模型带网关前缀（如 xiaomi/mimo-v2.6-flash）——**归一匹配**
  // 目录键（第九轮修复：此前精确匹配失败 ⇒ 挡位恒禁用，即使目录已有能力数据）
  const current = options.find(
    (option) =>
      option.name === model || normalizeModelKey(option.name) === normalizeModelKey(model),
  );
  const reasoning = Boolean(current?.supports_reasoning);
  const efforts = current?.reasoning_efforts ?? [];
  // -C：强制思考模型（Kimi / GLM-5.3 系）不出「关闭」档
  const offSupported = current?.supports_thinking_off ?? true;

  return (
    <div className="flex min-w-0 items-center gap-1.5">
      <ContextUsageRing sessionId={sessionId} refreshKey={usageRefreshKey} />
      <ThinkingSlider
        value={thinking}
        efforts={efforts}
        offSupported={offSupported}
        disabled={!reasoning}
        onChange={onThinking}
      />
      <ModelField
        model={model}
        options={options}
        activeProfile={activeProfile}
        profiles={profiles}
        employeeModel={employeeModel}
        onActivate={onActivateProfile}
        onOpenConfig={(mode) => {
          setConfigTarget(mode === "edit" ? activeProfile : null);
          setConfigOpen(true);
        }}
        openSignal={openSignal}
      />
      <ModelConfigDialog
        open={configOpen}
        onOpenChange={setConfigOpen}
        profile={configTarget}
        options={options}
        onChanged={() => onProfilesChanged()}
      />
    </div>
  );
}
