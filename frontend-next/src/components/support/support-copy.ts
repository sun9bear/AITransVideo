/**
 * Static copy strings used by SupportWidget.
 *
 * The greeting + quick-question seeds come from the gateway via
 * /api/support/config; these constants are the fallback values used
 * during the initial render (before the config fetch resolves) and the
 * extra static UI labels.
 */

export const FALLBACK_GREETING =
  "你好，我可以帮你解答试用、套餐、导出剪映草稿和任务排障问题。"

export const FALLBACK_QUICK_QUESTIONS = [
  "试用会自动扣费吗？",
  "怎么导出剪映草稿？",
  "任务失败怎么办？",
  "我要找人工客服",
]

export const SUPPORT_LABELS = {
  launcherLabel: "客服",
  launcherTooltip: "在线客服 / 帮助",
  panelTitle: "客服小助手",
  panelSubtitle: "AI 优先回答 · 解决不了会转人工",
  inputPlaceholder: "在这里描述你的问题…",
  sendButton: "发送",
  resolvedButton: "已解决",
  notResolvedButton: "没解决，转人工",
  closeButton: "关闭",
  newConversation: "开新对话",
  loading: "AI 正在思考…",
  budgetExhaustedNote: "AI 客服繁忙，已切换到模板回复。",
  handoffCreatedNote: "已创建人工工单",
  handoffFailedNote: "转人工失败，请稍后再试或直接发邮件",
  handoffWaitingNote: "已转人工，运营会通过邮件回复你",
  unauthHelp: "登录后可以让客服读取你的任务和账单上下文。",
}

export const ENTRYPOINT_FROM_PATH = (
  path: string | null | undefined,
):
  | "marketing_home"
  | "pricing"
  | "trial"
  | "contact"
  | "workspace"
  | "task_detail"
  | "billing"
  | "help"
  | "faq"
  | "notification"
  | "auth"
  | "unknown" => {
  if (!path) return "unknown"
  if (path === "/" || path === "") return "marketing_home"
  if (path.startsWith("/pricing")) return "pricing"
  if (path.startsWith("/trial")) return "trial"
  if (path.startsWith("/contact")) return "contact"
  if (path.startsWith("/help")) return "help"
  if (path.startsWith("/account/billing")) return "billing"
  if (path.startsWith("/notifications")) return "notification"
  if (path.startsWith("/workspace/")) return "task_detail"
  if (path.startsWith("/workspace")) return "workspace"
  if (path.startsWith("/login") || path.startsWith("/register"))
    return "auth"
  if (path.startsWith("/#faq") || path.includes("#faq")) return "faq"
  return "unknown"
}
