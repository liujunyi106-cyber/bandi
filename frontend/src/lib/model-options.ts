/**
 * 功能：模型候选合成——目录模型 ∪ 已配置档案模型。
 *
 * 背景：/api/settings/model 的 available_models 只含**模型目录**条目；网关私有模型
 * （不在目录里的自定义模型名）切不动，会话框模型控制条按目录过滤 → 已配置档案不可见
 * 用户切不动模型。此处把「带凭证的档案模型」并入候选，归一去重。
 */
import { normalizeModelKey } from "@/lib/model-key";
import type { ModelOption } from "@/services/settings";

/** 功能：合成候选只需档案的模型名与 provider（避免耦合掩码字段）。 */
export interface ConfiguredProfileRef {
  model: string;
  provider: string;
}

/**
 * 功能：把已配置档案模型并入目录候选（归一去重；档案优先保留目录条目）。
 */
export function mergeConfiguredProfiles(
  options: ModelOption[],
  profiles: ConfiguredProfileRef[],
): ModelOption[] {
  const known = new Set(options.map((option) => normalizeModelKey(option.name)));
  const extra: ModelOption[] = [];
  for (const profile of profiles) {
    const key = normalizeModelKey(profile.model);
    if (!key || known.has(key)) continue;
    known.add(key);
    extra.push({
      name: profile.model,
      provider: profile.provider,
      // 目录外模型的能力（思考挡位）由后端 /settings/model 动态补条目提供；
      // 此处仅作兜底合成，不虚报能力。
      supports_reasoning: false,
      reasoning_efforts: [],
    });
  }
  return extra.length ? [...options, ...extra] : options;
}

/** 功能：只保留已配置档案（或全局凭证）覆盖的模型，供员工创建/编辑使用。 */
export function configuredModelOptions(
  options: ModelOption[],
  profiles: ConfiguredProfileRef[],
  globalModel: string,
  globalHasKey: boolean,
): ModelOption[] {
  const keys = new Set(
    profiles.map((profile) => normalizeModelKey(profile.model)).filter(Boolean),
  );
  if (globalHasKey && globalModel) keys.add(normalizeModelKey(globalModel));

  const byKey = new Map(options.map((option) => [normalizeModelKey(option.name), option]));
  const result: ModelOption[] = [];
  for (const key of keys) {
    const option = byKey.get(key);
    if (option) {
      result.push(option);
      continue;
    }
    const profile = profiles.find((item) => normalizeModelKey(item.model) === key);
    if (profile) {
      result.push({
        name: profile.model,
        provider: profile.provider,
        supports_reasoning: false,
        reasoning_efforts: [],
      });
    }
  }
  return result;
}
