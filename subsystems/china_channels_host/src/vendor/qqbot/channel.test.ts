import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  monitorQQBotProvider: vi.fn().mockResolvedValue(undefined),
  stopQQBotMonitorForAccount: vi.fn(),
  setQQBotRuntime: vi.fn(),
}));

vi.mock("./monitor.js", () => ({
  monitorQQBotProvider: mocks.monitorQQBotProvider,
  stopQQBotMonitorForAccount: mocks.stopQQBotMonitorForAccount,
}));

vi.mock("./runtime.js", () => ({
  setQQBotRuntime: mocks.setQQBotRuntime,
}));

import { qqbotPlugin } from "./channel.js";

describe("qqbotPlugin capabilities", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("declares direct, group, and channel chat types", () => {
    expect(qqbotPlugin.capabilities.chatTypes).toEqual(["direct", "group", "channel"]);
  });

  it("accepts runtimes that only expose the direct reply dispatcher", async () => {
    const runtime = {
      channel: {
        routing: {
          resolveAgentRoute: vi.fn(),
        },
        reply: {
          dispatchReplyWithDispatcher: vi.fn(),
        },
      },
    };

    await qqbotPlugin.gateway.startAccount({
      cfg: {
        channels: {
          qqbot: {
            appId: "app-1",
            clientSecret: "secret-1",
          },
        },
      },
      runtime,
      accountId: "default",
      log: {
        info: vi.fn(),
        error: vi.fn(),
      },
    });

    expect(mocks.setQQBotRuntime).toHaveBeenCalledWith(runtime);
    expect(mocks.monitorQQBotProvider).toHaveBeenCalledWith(
      expect.objectContaining({
        config: {
          channels: {
            qqbot: {
              appId: "app-1",
              clientSecret: "secret-1",
            },
          },
        },
        runtime,
        accountId: "default",
      })
    );
  });
});
