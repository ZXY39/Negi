const CRON_HIDDEN_PROMPT = `
When creating or updating a cron task, always store a fixed delivery target in the job itself.
- Use the built-in cron tool (action=add/update). Do not run shell commands.
- Must use sessionTarget="isolated" for reminder jobs.
- payload.kind="agentTurn"
- payload.message must be plain user-visible reminder text only.
- You must encode runtime guardrails directly into payload.message so the cron run can follow them without extra context.
- Runtime guardrails to encode in payload.message:
  - return plain text only
  - never call any tool
  - never call the message tool
  - never send manually; delivery is handled by cron delivery settings
- Do not include tool directives, "NO_REPLY", or heartbeat markers in payload.message.
- Job name is never a message target.
- During cron run, must return plain text only and never call the message tool.
- Use top-level delivery with announce mode:
  delivery.mode="announce"
  delivery.channel=<OriginatingChannel> (example: "qqbot")
  delivery.to=<OriginatingTo> (examples: "user:<openid>" / "group:<group_openid>")
  delivery.accountId=<AccountId> when available
- Never set delivery.channel="last" for multi-channel environments.
- If OriginatingChannel/OriginatingTo are unavailable, ask a concise follow-up for channel and target.
- Do not call the message tool to send`;

const CRON_TRIGGER_KEYWORDS = [
  "定时",
  "提醒",
  "每分钟",
  "每小时",
  "每天",
  "每周",
  "几点",
  "早上",
  "晚上",
  "工作日",
  "cron",
  "remind",
  "reminder",
  "schedule",
  "scheduled",
  "every minute",
  "every hour",
  "every day",
  "daily",
  "every week",
  "weekly",
  "weekday",
  "workday",
  "morning",
  "evening",
];

const CRON_TRIGGER_PATTERNS = [
  /提醒我/u,
  /帮我定时/u,
  /每.+提醒/u,
  /每天.+发/u,
  /remind me/iu,
  /set (a )?reminder/iu,
  /every .+ remind/iu,
  /every day .+ (send|post|notify)/iu,
  /schedule .+ (reminder|message|notification)/iu,
];

const CRON_EXCLUDE_PATTERNS = [
  /是什么意思/u,
  /区别/u,
  /为什么/u,
  /\bhelp\b/iu,
  /文档/u,
  /怎么用/u,
  /what does|what's|meaning of/iu,
  /difference/iu,
  /why/iu,
  /\bdocs?\b/iu,
  /documentation/iu,
  /how to/iu,
  /usage/iu,
];

export function shouldInjectCronHiddenPrompt(text: string): boolean {
  const normalized = text.trim();
  if (!normalized) return false;
  const lowered = normalized.toLowerCase();

  for (const pattern of CRON_EXCLUDE_PATTERNS) {
    if (pattern.test(lowered)) return false;
  }

  for (const keyword of CRON_TRIGGER_KEYWORDS) {
    if (lowered.includes(keyword.toLowerCase())) return true;
  }

  return CRON_TRIGGER_PATTERNS.some((pattern) => pattern.test(normalized));
}

export function splitCronHiddenPrompt(text: string): { base: string; prompt?: string } {
  const idx = text.indexOf(CRON_HIDDEN_PROMPT);
  if (idx === -1) {
    return { base: text };
  }
  const base = text.slice(0, idx).trimEnd();
  return { base, prompt: CRON_HIDDEN_PROMPT };
}

export function appendCronHiddenPrompt(text: string): string {
  if (!shouldInjectCronHiddenPrompt(text)) return text;
  if (text.includes(CRON_HIDDEN_PROMPT)) return text;
  return `${text}\n\n${CRON_HIDDEN_PROMPT}`;
}

export function applyCronHiddenPromptToContext<
  T extends { Body?: string; RawBody?: string; CommandBody?: string }
>(ctx: T): boolean {
  const base =
    (typeof ctx.RawBody === "string" && ctx.RawBody) ||
    (typeof ctx.Body === "string" && ctx.Body) ||
    (typeof ctx.CommandBody === "string" && ctx.CommandBody) ||
    "";

  if (!base) return false;

  const next = appendCronHiddenPrompt(base);
  if (next === base) return false;

  ctx.CommandBody = next;
  return true;
}

export { CRON_HIDDEN_PROMPT };
