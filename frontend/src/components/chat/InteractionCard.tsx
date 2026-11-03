/**
  * 功能：统一决策卡
 * -3a 起为「统一决策区」内卡片——呈现在输入框上方）。
 * 分流（payload 优先）：
 * - `ui_variant=permission` → 权限三态（仅本次允许 / 本会话始终允许 / 拒绝）→ approval-decision 端点；
 * - `payload.notify` → 非交互提示（无按钮/无倒计时——）；
 * - choices 选项（单选/多选 multi_select + allow_custom → clarify 应答）；
 * - 无 choices → 允许/拒绝两态（confirm）或 input 文本（clarify）；
  * 倒计时。
 */
import { ChevronLeft, ChevronRight, Info, ListChecks, ShieldQuestion } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { ApiError } from "@/services/api";
import type { InteractionFrame } from "@/services/sessions";
import { sessionsApi } from "@/services/sessions";

interface InteractionCardProps {
  frame: InteractionFrame;
  onResolved: (interactionId: string) => void;
  onError: (message: string) => void;
  /**
    * 功能：作答草稿（①）。
   * 多张决策卡并存时用户会切走再切回，卡片一旦卸载其本地作答即丢失；
   * 由父级按 interaction_id 分桶持久化，切回时经 initialDraft 复原。
   * 缺省时组件自持草稿（单卡场景与单测无需父级参与）。
   */
  initialDraft?: InteractionDraft;
  onDraftChange?: (interactionId: string, draft: InteractionDraft) => void;
  /** 功能：多卡并存时的卡序（从卡外挪进卡内，置于倒计时左侧，形如 1/2）。 */
  cardIndex?: number;
  cardTotal?: number;
  onPrevCard?: () => void;
  onNextCard?: () => void;
}

/** 功能：一张决策卡的作答草稿（不含 pending 等瞬时态）。 */
export interface InteractionDraft {
  answer: string;
  custom: string;
  picked: string[];
  page: number;
  answers: Record<string, string>;
}

const EMPTY_INTERACTION_DRAFT: InteractionDraft = {
  answer: "",
  custom: "",
  picked: [],
  page: 0,
  answers: {},
};

function formatCountdown(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

/** 功能：挂起剩余秒数倒计时（帧缺 expires_in 时返回 null 不显示）。 */
function useCountdown(expiresIn?: number): number | null {
  const [remaining, setRemaining] = useState<number | null>(
    typeof expiresIn === "number" ? expiresIn : null,
  );
  useEffect(() => {
    if (typeof expiresIn !== "number") {
      setRemaining(null);
      return;
    }
    setRemaining(expiresIn);
    const timer = window.setInterval(() => {
      setRemaining((previous) => (previous === null ? null : Math.max(0, previous - 1)));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [expiresIn]);
  return remaining;
}

function frameSummary(frame: InteractionFrame): string {
  const payload = frame.payload as Record<string, unknown>;
  // 权限卡可读摘要：工具名 + 关键参数摘录——无 message 字段时
  // 回退显示 kind 字面值「permission」
  if (payload?.ui_variant === "permission" || frame.kind === "permission") {
    const tool = String(payload?.tool_name || "").trim();
    if (tool) {
      const args = (payload?.args || {}) as Record<string, unknown>;
      const brief = Object.values(args)
        .map((value) => String(value))
        .join(" ")
        .replace(/\s+/g, " ")
        .trim()
        .slice(0, 150);
      return brief ? `${tool} · ${brief}` : tool;
    }
  }
  for (const key of ["message", "prompt", "question", "detail"]) {
    const value = payload?.[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return frame.kind;
}

function frameChoices(frame: InteractionFrame): string[] {
  const payload = frame.payload as Record<string, unknown>;
  const raw = payload?.choices;
  if (!Array.isArray(raw)) return [];
  return raw.map((item) => String(item));
}

export function InteractionCard({
  frame,
  onResolved,
  onError,
  initialDraft,
  onDraftChange,
  cardIndex,
  cardTotal,
  onPrevCard,
  onNextCard,
}: InteractionCardProps) {
  const t = useT();
  const [draft, setDraft] = useState<InteractionDraft>(initialDraft ?? EMPTY_INTERACTION_DRAFT);
  const [pending, setPending] = useState(false);
  // 父级回流按 id 变化读取，避免把每键重建的草稿对象当依赖（否则输入中反复重挂）
  const latestDraft = useRef(initialDraft);
  latestDraft.current = initialDraft;
  useEffect(() => {
    setDraft(latestDraft.current ?? EMPTY_INTERACTION_DRAFT);
  }, [frame.interaction_id]);

  // 上报父级必须在 updater 外：updater 可能于渲染期执行，在其中调父级 setState
  // 会触发 React「渲染中更新其它组件」告警并在并发模式下不可靠
  const update = (patch: Partial<InteractionDraft>) => {
    const next = { ...draft, ...patch };
    setDraft(next);
    onDraftChange?.(frame.interaction_id, next);
  };
  const { answer, custom, picked, page, answers } = draft;

  const payload = frame.payload as Record<string, unknown>;
  const payloadToolCallId =
    typeof payload.tool_call_id === "string" ? payload.tool_call_id : undefined;
  const choices = frameChoices(frame);
  const multi = payload?.multi_select === true;
  // 选项卡**默认带自定义输入框**（此前需 Agent 显式传 allow_custom
  // 实际几乎不传 → 卡片只有按钮，用户想补充文字却无处输入）；显式 false 才隐藏。
  const allowCustom = payload?.allow_custom !== false;
  const isInput = frame.kind === "input";
  // -3a：permission 卡走三态决策端点；notify 为非交互提示（无操作区）
  const isPermission = payload?.ui_variant === "permission";
  // 批量权限卡：一次决策覆盖多项高危调用（items 由 executor 预检聚合）
  const batchItems = Array.isArray(payload?.items)
    ? (payload.items as Array<Record<string, unknown>>)
    : [];
  const isBatchPermission = isPermission && batchItems.length > 1;
  // 卡内最多列 3 条，其余折叠为「还有 N 项」——决策仍对全部 items 生效
  const visibleBatchItems = batchItems.slice(0, 3);
  const hiddenBatchCount = batchItems.length - visibleBatchItems.length;
  // 员工 headless 审批（ApprovalStore）与交互卡（InteractionStore）
  // 是两条通道——approval_store 来源经 approval-decision 端点决策（session 级 sid）；
  // 其余维持 confirm 端点（交互卡 pending）。
  const isApprovalStore = payload?.source === "approval_store";
  const permissionConfirm = (
    allowed: boolean,
    feedback?: string,
    scope?: "session" | "once",
  ): Promise<unknown> =>
    isApprovalStore
      ? sessionsApi.approvalDecision(
          frame.session_id,
          !allowed ? "deny" : scope === "session" ? "allow_session" : "allow_once",
          feedback,
          payloadToolCallId,
        )
      : scope
        ? sessionsApi.confirm(frame.session_id, frame.interaction_id, allowed, feedback, scope)
        : sessionsApi.confirm(frame.session_id, frame.interaction_id, allowed, feedback);
  const isNotify = payload?.notify === true;
  const detailText = typeof payload?.detail === "string" ? payload.detail.trim() : "";
  const rulePattern = typeof payload?.rule_pattern === "string" ? payload.rule_pattern.trim() : "";
  const ruleSource = typeof payload?.rule_source === "string" ? payload.rule_source.trim() : "";
  const ruleText = rulePattern || ruleSource ? [rulePattern, ruleSource].filter(Boolean).join(" · ") : "";
  // 多决策点（questions 数组）→ 分页模式
  const questions = Array.isArray(payload?.questions)
    ? (payload.questions as Array<Record<string, unknown>>)
    : [];
  const paged = questions.length > 1;
  const currentQuestion = paged ? questions[Math.min(page, questions.length - 1)] : undefined;
  // 题目键与「全题已答」校验使用同一派生口径（生产形态题无 id 时回退 q{序}）
  const questionId = (question: Record<string, unknown> | undefined, index: number): string =>
    String(question?.id ?? `q${index + 1}`);
  const currentQid = questionId(currentQuestion, page);
  const allAnswered = questions.every((question, index) => {
    const value = answers[questionId(question, index)];
    return typeof value === "string" && value.trim().length > 0;
  });
  // 多问时**允许未答完就确定**（可先跳到后面几题，回头再补；
  // 没想好的一道不影响其余）。未答的题不进 answers → LLM 收到部分答复
  // 由它判断该不该继续追问（缺失项即"未决问题"）。
  const hasAnyAnswer = Object.values(answers).some(
    (value) => typeof value === "string" && value.trim().length > 0,
  );
  const currentChoices = Array.isArray(currentQuestion?.choices)
    ? (currentQuestion?.choices as unknown[]).map((item) => String(item))
    : [];
  // 有选项时并存的自由输入框与选项**共用同一答案键**——
  // 当前答案恰为某选项时输入框置空（避免同一内容两处显示），手打即改写该答案。
  const currentAnswer = answers[currentQid] ?? "";
  const currentAnswerIsChoice = currentChoices.includes(currentAnswer);
  const remaining = useCountdown(frame.expires_in);
  // 功能：倒计时到点自动收起卡片——后端 TTL 已过期，卡片不应永久挂在输入框上方
  useEffect(() => {
    if (remaining === null || remaining > 0) return;
    const timer = window.setTimeout(() => onResolved(frame.interaction_id), 1200);
    return () => window.clearTimeout(timer);
  }, [remaining, frame.interaction_id, onResolved]);
  // 孤儿卡（执行已结束、答复无处唤醒——切会话重放后仍反复出现且重答无效果）：
  // 视同已失效——标注说明并禁用全部操作
  const orphaned = frame.orphaned === true;
  const expired = (remaining !== null && remaining <= 0) || orphaned;

  // （二次）：卡序与题序合成**单一翻页器**。
  // 点 › 前进一题（1/2 → 2/2），本卡最后一题再点 → 切到下一张卡（显示其 1/N）；
  // ‹ 反向同理（首题 → 上一张卡）。不再并存两组 n/m（会被读成「2/2 1/2」）。
  const questionIndex = Math.min(page, Math.max(0, questions.length - 1));
  const canNextQuestion = questions.length > 1 && questionIndex < questions.length - 1;
  const canPrevQuestion = questions.length > 1 && questionIndex > 0;
  const canNextCard = (cardIndex ?? 0) < (cardTotal ?? 1) - 1;
  const canPrevCard = (cardIndex ?? 0) > 0;
  const canNextPage = canNextQuestion || canNextCard;
  const canPrevPage = canPrevQuestion || canPrevCard;
  // 多题分页：主按钮在非末题 = 「下一题」（翻页，不提交）——防误提交
  // （用户易把「确定」误读为翻页而漏答后续题）
  const isLastQuestion = !paged || questionIndex >= questions.length - 1;
  // （三次）：**没有翻页数字就不显示 ‹ ›**——箭头只在存在 n/m 时出现。
  // 故无题卡（权限卡）完全不渲染翻页器；有题卡在「多题」或「多卡需切换」时渲染。
  const showPager = questions.length > 1 || (questions.length > 0 && (cardTotal ?? 1) > 1);
  const goNextPage = () => {
    if (canNextQuestion) update({ page: questionIndex + 1 });
    else if (canNextCard) onNextCard?.();
  };
  const goPrevPage = () => {
    if (canPrevQuestion) update({ page: questionIndex - 1 });
    else if (canPrevCard) onPrevCard?.();
  };

  const respond = async (action: () => Promise<unknown>) => {
    setPending(true);
    try {
      await action();
      onResolved(frame.interaction_id);
    } catch (error) {
      onError(error instanceof ApiError ? error.message : String(error));
    } finally {
      setPending(false);
    }
  };

  const submitClarify = (value: string) =>
    void respond(() => sessionsApi.clarify(frame.session_id, frame.interaction_id, value));

  const cancelButton = (
    <Button
      size="sm"
      variant="outline"
      className="h-7 px-3 text-xs"
      data-testid="interaction-cancel"
      disabled={pending || expired}
      onClick={() => submitClarify("")}
    >
      {t("chat.interaction.cancel")}
    </Button>
  );

  const togglePick = (choice: string) => {
    update({
      picked: multi
        ? picked.includes(choice)
          ? picked.filter((item) => item !== choice)
          : [...picked, choice]
        : [choice],
      custom: "",
    });
  };

  return (
    <div
      className={cn(
        // 无描边（背景色区分）；宽度比输入框略大（-mx-1 含住）；
        // 作为决策区最后一张时底直边贴合输入框
        "-mx-1 mb-2 rounded-xl px-3 py-2.5 last:mb-0 last:rounded-b-none",
        // 交互卡不自铺底色——外层壳（Composer 的 aboveTone）提供同色底；
        // notify 无外壳语境，保留中性底
        isNotify ? "bg-chat-rail/60" : "",
      )}
      data-testid="interaction-card"
    >
      <div className="text-chat-ink flex items-start gap-2 text-sm">
        {isNotify ? (
          <Info className="text-chat-muted mt-0.5 size-4 shrink-0" />
        ) : isPermission ? (
          <ShieldQuestion className="text-primary mt-0.5 size-4 shrink-0" />
        ) : (
          // 提问/选项卡卡换用 ListChecks（原先与权限卡同为
          // ShieldQuestion，两者语义不同却同图标）；同 lucide 同尺寸同色，风格一致。
          <ListChecks className="text-primary mt-0.5 size-4 shrink-0" />
        )}
        <div className="min-w-0 flex-1">
          {isNotify ? (
            <p className="text-chat-muted text-xs break-words">{frameSummary(frame)}</p>
          ) : (
            <>
              <p className="font-medium">{t("chat.interaction.title")}</p>
              <p className="text-chat-muted mt-0.5 text-xs break-words">
                {isBatchPermission
                  ? t("chat.interaction.batchSummary").replace(
                      "{count}",
                      String(batchItems.length),
                    )
                  : frameSummary(frame)}
              </p>
              {detailText && detailText !== frameSummary(frame) ? (
                <p className="text-chat-muted mt-0.5 text-[11px] break-words">{detailText}</p>
              ) : null}
              {ruleText ? (
                <p
                  data-testid="interaction-rule"
                  className="text-chat-muted mt-0.5 font-mono text-[11px] break-words"
                >
                  {t("chat.interaction.ruleSource")}: {ruleText}
                </p>
              ) : null}
              {orphaned ? (
                <p
                  className="text-destructive mt-1 text-[11px] break-words"
                  data-testid="interaction-orphaned"
                >
                  {t("chat.interaction.orphaned")}
                </p>
              ) : null}
            </>
          )}
        </div>
        {!isNotify && (questions.length > 0 || remaining !== null || (cardTotal ?? 0) > 1) ? (
          <div className="flex shrink-0 items-center gap-1.5">
            {showPager ? (
              <>
                <button
                  type="button"
                  data-testid="decision-prev"
                  disabled={!canPrevPage}
                  className="text-chat-muted hover:text-chat-ink disabled:opacity-50"
                  onClick={goPrevPage}
                >
                  <ChevronLeft className="size-3.5" />
                </button>
                <span
                  data-testid="decision-page"
                  className="text-chat-muted text-[11px] tabular-nums"
                >
                  {questionIndex + 1}/{questions.length}
                </span>
                <button
                  type="button"
                  data-testid="decision-next"
                  disabled={!canNextPage}
                  className="text-chat-muted hover:text-chat-ink disabled:opacity-50"
                  onClick={goNextPage}
                >
                  <ChevronRight className="size-3.5" />
                </button>
              </>
            ) : null}
            {remaining !== null ? (
              <span
                data-testid="interaction-countdown"
                data-expired={expired ? "true" : "false"}
                className={cn(
                  "shrink-0 text-[11px] tabular-nums",
                  expired ? "text-destructive" : "text-chat-muted",
                )}
              >
                {expired ? t("chat.interaction.expired") : formatCountdown(remaining)}
              </span>
            ) : null}
          </div>
        ) : null}
      </div>

      {isNotify ? null : paged ? (
        <div className="mt-2 flex flex-col gap-1">
          <p className="text-chat-ink px-0.5 text-xs">{String(currentQuestion?.question ?? "")}</p>
          {currentChoices.length > 0
            ? currentChoices.map((choice) => {
                const selected = answers[currentQid] === choice;
                return (
                  <button
                    key={choice}
                    type="button"
                    data-testid="interaction-choice"
                    disabled={pending || expired}
                    className={cn(
                      // 选项条**恒为卡片白底、无描边**；选中**只变左侧
                      // 圆圈**（行本身不换底/不换字色），悬浮仅文字加深。
                      "bg-card text-chat-muted hover:text-chat-ink flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-xs",
                    )}
                    onClick={() => update({ answers: { ...answers, [currentQid]: choice } })}
                  >
                    <span
                      className={cn(
                        "size-3.5 shrink-0 rounded-full border",
                        selected ? "border-primary bg-primary" : "border-chat-hairline",
                      )}
                    />
                    <span className="min-w-0 flex-1 truncate">{choice}</span>
                  </button>
                );
              })
            : null}
          {/*有选项时**同时**保留自由输入框（选项覆盖不到的答案直接手打）；
              无选项时必给输入框（否则无从作答）。allow_custom=false 且带选项时才只留按钮 */}
          {allowCustom || currentChoices.length === 0 ? (
            <input
              data-testid="decision-input"
              className="bg-card text-chat-ink min-w-0 rounded-lg px-2.5 py-1.5 text-xs outline-none"
              placeholder={t("chat.interaction.answerPlaceholder")}
              value={currentAnswerIsChoice ? "" : currentAnswer}
              disabled={pending || expired}
              // 功能：聚焦自由输入框即视为要手打答案——自动取消已选选项
              onFocus={() => {
                if (currentAnswerIsChoice) update({ answers: { ...answers, [currentQid]: "" } });
              }}
              onChange={(event) =>
                update({ answers: { ...answers, [currentQid]: event.target.value } })
              }
            />
          ) : null}
          {/*去掉「（部分作答）」按钮备注与左下角未答提示——
              确定按钮统一文案，未答语义由 Agent 侧 note 处理，不在卡上提示用户 */}
          <div className="mt-1 flex items-center justify-end gap-2">
            {cancelButton}
            <Button
              size="sm"
              className="h-7 px-3 text-xs"
              data-testid="interaction-submit"
              // 多题分页：非末题点击 = 翻到下一题（不提交）；末题才真正提交
              // （至少答一题或全答完可提交——部分作答语义由 Agent 侧 note 处理）
              disabled={pending || expired || (isLastQuestion && !allAnswered && !hasAnyAnswer)}
              onClick={() => {
                if (!isLastQuestion) {
                  goNextPage();
                  return;
                }
                void respond(() =>
                  sessionsApi.clarify(
                    frame.session_id,
                    frame.interaction_id,
                    JSON.stringify(answers),
                  ),
                );
              }}
            >
              {isLastQuestion ? t("common.confirm") : t("chat.interaction.nextQuestion")}
            </Button>
          </div>
        </div>
      ) : isPermission ? (
        <div className="mt-2 flex flex-col gap-2">
          {isBatchPermission ? (
            <ul data-testid="permission-batch-list" className="flex flex-col gap-1">
              {visibleBatchItems.map((batchItem, index) => {
                const tool = String(batchItem.tool_name || "").trim() || `#${index + 1}`;
                const args = (batchItem.args || {}) as Record<string, unknown>;
                const brief = Object.values(args)
                  .map((value) => String(value))
                  .join(" ")
                  .replace(/\s+/g, " ")
                  .trim()
                  .slice(0, 90);
                return (
                  <li
                    key={`${tool}-${index}`}
                    className="text-chat-muted font-mono text-[11px] break-words"
                  >
                    {index + 1}. {tool}
                    {brief ? ` · ${brief}` : ""}
                  </li>
                );
              })}
              {hiddenBatchCount > 0 ? (
                <li
                  data-testid="permission-batch-more"
                  className="text-chat-muted/70 text-[11px] break-words"
                >
                  {t("chat.interaction.batchMore").replace("{count}", String(hiddenBatchCount))}
                </li>
              ) : null}
            </ul>
          ) : null}
          <div className="flex flex-wrap justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              data-testid="permission-deny"
              disabled={pending || expired}
              onClick={() => void respond(() => permissionConfirm(false))}
            >
              {t("chat.interaction.deny")}
            </Button>
            <Button
              variant="outline"
              size="sm"
              data-testid="permission-allow-session"
              disabled={pending || expired}
              onClick={() =>
                void respond(() =>
                  // 交互卡走 confirm 端点 + scope=session；起
                  // approval_store 来源改走 approval-decision（permissionConfirm 内分流）。
                  permissionConfirm(true, undefined, "session"),
                )
              }
            >
              {t("chat.interaction.allowSession")}
            </Button>
            <Button
              size="sm"
              data-testid="permission-allow-once"
              disabled={pending || expired}
              onClick={() => void respond(() => permissionConfirm(true, undefined, "once"))}
            >
              {t("chat.interaction.allowOnce")}
            </Button>
          </div>
        </div>
      ) : choices.length > 0 ? (
        <div className="mt-2 flex flex-col gap-1">
          {choices.map((choice) => {
            const selected = picked.includes(choice);
            return (
              <button
                key={choice}
                type="button"
                data-testid="interaction-choice"
                disabled={pending || expired}
                className={cn(
                  // 与多问分页分支**统一**——选项条恒白底、无描边
                  // 选中只变左侧圆圈（此前该分支还有 border + hover 描边加强 + 选中换底/换字色）。
                  "bg-card text-chat-muted hover:text-chat-ink flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-xs",
                )}
                onClick={() => togglePick(choice)}
              >
                <span
                  className={cn(
                    "size-3.5 shrink-0 border",
                    multi ? "rounded" : "rounded-full",
                    selected ? "border-primary bg-primary" : "border-chat-hairline",
                  )}
                />
                <span className="min-w-0 flex-1 truncate">{choice}</span>
              </button>
            );
          })}
          {allowCustom ? (
            <input
              data-testid="decision-input"
              className="bg-card text-chat-ink min-w-0 rounded-lg px-2.5 py-1.5 text-xs outline-none"
              placeholder={t("chat.interaction.answerPlaceholder")}
              value={custom}
              disabled={pending || expired}
              // 功能：聚焦自由输入框即视为要手打答案——自动取消已选选项
              onFocus={() => {
                if (picked.length) update({ picked: [] });
              }}
              onChange={(event) => {
                update({ custom: event.target.value, picked: [] });
              }}
            />
          ) : null}
          <div className="mt-1 flex justify-end gap-2">
            {cancelButton}
            <Button
              size="sm"
              className="h-7 px-3 text-xs"
              data-testid="interaction-submit"
              disabled={pending || expired || (!custom.trim() && picked.length === 0)}
              onClick={() => submitClarify(custom.trim() || picked.join("、"))}
            >
              {t("common.confirm")}
            </Button>
          </div>
        </div>
      ) : isInput ? (
        <div className="mt-2 flex gap-2">
          <input
            data-testid="decision-input"
            className="border-chat-hairline bg-card text-chat-ink focus:border-primary/60 min-w-0 flex-1 rounded-md border px-2 py-1.5 text-sm outline-none"
            value={answer}
            placeholder={t("chat.interaction.answerPlaceholder")}
            disabled={pending || expired}
            onChange={(event) => update({ answer: event.target.value })}
          />
          {cancelButton}
          <Button
            size="sm"
            data-testid="interaction-submit"
            disabled={pending || expired || !answer.trim()}
            onClick={() =>
              void respond(() =>
                sessionsApi.clarify(frame.session_id, frame.interaction_id, answer.trim()),
              )
            }
          >
            {t("common.confirm")}
          </Button>
        </div>
      ) : (
        <div className="mt-2 flex flex-col gap-2">
          <div className="flex justify-end gap-2">
            <Button
              variant="outline"
              size="sm"
              data-testid="interaction-deny"
              disabled={pending || expired}
              onClick={() =>
                void respond(() =>
                  sessionsApi.confirm(frame.session_id, frame.interaction_id, false),
                )
              }
            >
              {t("chat.interaction.deny")}
            </Button>
            <Button
              size="sm"
              data-testid="interaction-allow"
              disabled={pending || expired}
              onClick={() =>
                void respond(() =>
                  sessionsApi.confirm(frame.session_id, frame.interaction_id, true),
                )
              }
            >
              {t("chat.interaction.allow")}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
