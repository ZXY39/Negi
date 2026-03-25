import { describe, expect, it } from "vitest";
import {
  appendCronHiddenPrompt,
  applyCronHiddenPromptToContext,
  shouldInjectCronHiddenPrompt,
  splitCronHiddenPrompt,
} from "./index.js";

describe("cron hidden prompt", () => {
  it("injects fixed delivery guidance for reminder-like messages", () => {
    const text = "请帮我每小时提醒喝水";
    const next = appendCronHiddenPrompt(text);

    expect(shouldInjectCronHiddenPrompt(text)).toBe(true);
    expect(next).not.toBe(text);
    expect(next).toContain('sessionTarget="isolated"');
    expect(next).toContain('payload.kind="agentTurn"');
    expect(next).toContain("Use the built-in cron tool (action=add/update)");
    expect(next).toContain("payload.message must be plain user-visible reminder text only");
    expect(next).toContain('delivery.mode="announce"');
    expect(next).toContain("delivery.channel=<OriginatingChannel>");
    expect(next).toContain("delivery.to=<OriginatingTo>");
    expect(next).toContain('Never set delivery.channel="last"');
  });

  it("does not inject for documentation-style questions", () => {
    const text = "cron 文档怎么用？";
    const next = appendCronHiddenPrompt(text);

    expect(shouldInjectCronHiddenPrompt(text)).toBe(false);
    expect(next).toBe(text);
  });

  it("is idempotent when called repeatedly", () => {
    const text = "每分钟提醒我伸展一下";
    const once = appendCronHiddenPrompt(text);
    const twice = appendCronHiddenPrompt(once);

    expect(twice).toBe(once);
  });

  it("can split injected prompt from body", () => {
    const injected = appendCronHiddenPrompt("set a reminder every day at 9am");
    const result = splitCronHiddenPrompt(injected);

    expect(result.base).toBe("set a reminder every day at 9am");
    expect(result.prompt).toBeDefined();
  });

  it("applies to context command body only when needed", () => {
    const ctx: { Body?: string; CommandBody?: string } = { Body: "每天 18:00 提醒我下班喝水" };
    const changed = applyCronHiddenPromptToContext(ctx);

    expect(changed).toBe(true);
    expect(ctx.CommandBody).toContain("delivery.mode=\"announce\"");
  });
});
