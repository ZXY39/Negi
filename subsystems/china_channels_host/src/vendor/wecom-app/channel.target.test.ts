import { describe, expect, it } from "vitest";

import { wecomAppPlugin } from "./channel.js";

describe("wecom-app target resolution", () => {
  it("accepts explicit user target", () => {
    expect(wecomAppPlugin.directory.canResolve({ target: "user:zhangchongwen" })).toBe(true);
    expect(
      wecomAppPlugin.directory.resolveTarget({
        cfg: {},
        target: "user:zhangchongwen",
      })
    ).toEqual({
      channel: "wecom-app",
      to: "zhangchongwen",
      accountId: undefined,
    });
  });

  it("accepts bare lowercase userId but rejects mixed-case display-like bare target", () => {
    expect(
      wecomAppPlugin.directory.resolveTarget({
        cfg: {},
        target: "zhangchongwen",
      })
    ).toEqual({
      channel: "wecom-app",
      to: "zhangchongwen",
      accountId: undefined,
    });

    expect(wecomAppPlugin.directory.canResolve({ target: "ZhangChongWen" })).toBe(false);
    expect(
      wecomAppPlugin.directory.resolveTarget({
        cfg: {},
        target: "ZhangChongWen",
      })
    ).toBeNull();
  });

  it("accepts explicit user prefix for mixed-case legacy userId", () => {
    expect(
      wecomAppPlugin.directory.resolveTarget({
        cfg: {},
        target: "user:ZhangChongWen",
      })
    ).toEqual({
      channel: "wecom-app",
      to: "ZhangChongWen",
      accountId: undefined,
    });
  });
});

