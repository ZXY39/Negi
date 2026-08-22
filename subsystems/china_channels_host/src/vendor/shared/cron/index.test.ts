import { describe, expect, it } from "vitest";
import {
  CRON_HIDDEN_PROMPT,
  appendCronHiddenPrompt,
  applyCronHiddenPromptToContext,
  shouldInjectCronHiddenPrompt,
  splitCronHiddenPrompt,
} from "./index.js";

describe("cron hidden prompt", () => {
  it("no longer appends the hidden prompt to user messages", () => {
    const text = "请帮我每小时提醒喝水";
    const next = appendCronHiddenPrompt(text);

    // 意图识别仍存在，但注入已停用（规范改由 cron toolskill 按需加载）。
    expect(shouldInjectCronHiddenPrompt(text)).toBe(true);
    expect(next).toBe(text);
    expect(next).not.toContain(CRON_HIDDEN_PROMPT);
  });

  it("applyCronHiddenPromptToContext leaves the command body untouched", () => {
    const ctx: { Body?: string; CommandBody?: string } = { Body: "每天 18:00 提醒我下班喝水" };
    const changed = applyCronHiddenPromptToContext(ctx);

    expect(changed).toBe(false);
    expect(ctx.CommandBody ?? "").not.toContain("delivery.mode=\"announce\"");
  });

  it("still splits previously-polluted bodies for backward compatibility", () => {
    const polluted = `set a reminder every day at 9am\n\n${CRON_HIDDEN_PROMPT}`;
    const result = splitCronHiddenPrompt(polluted);

    expect(result.base).toBe("set a reminder every day at 9am");
    expect(result.prompt).toBeDefined();
  });
});
