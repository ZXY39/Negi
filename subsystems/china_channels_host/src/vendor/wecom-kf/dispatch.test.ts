import { afterEach, describe, expect, it, vi } from "vitest";

import { clearAllAccessTokenCache } from "./api.js";
import { dispatchKfMessage } from "./dispatch.js";
import type { PluginConfig, PluginRuntime, ResolvedWecomKfAccount, SyncMsgText } from "./types.js";

function createAccount(): ResolvedWecomKfAccount {
  return {
    accountId: "default",
    enabled: true,
    configured: true,
    token: "callback-token",
    encodingAESKey: "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
    corpId: "ww-test-corp",
    corpSecret: "kf-secret",
    openKfId: "wk-test",
    canSendActive: true,
    config: {
      webhookPath: "/wecom-kf",
      token: "callback-token",
      encodingAESKey: "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
      corpId: "ww-test-corp",
      corpSecret: "kf-secret",
      openKfId: "wk-test",
    },
  };
}

function createRuntime(
  dispatchReplyWithBufferedBlockDispatcher: NonNullable<
    NonNullable<PluginRuntime["channel"]>["reply"]
  >["dispatchReplyWithBufferedBlockDispatcher"]
): PluginRuntime {
  return {
    channel: {
      routing: {
        resolveAgentRoute: () => ({
          sessionKey: "session-1",
          accountId: "default",
          agentId: "agent-1",
        }),
      },
      reply: {
        dispatchReplyWithBufferedBlockDispatcher,
      },
    },
  };
}

function createTextMessage(): SyncMsgText {
  return {
    msgid: "msg-1",
    msgtype: "text",
    origin: 3,
    open_kfid: "wk-test",
    external_userid: "wx-user-1",
    send_time: Date.now(),
    text: {
      content: "hello from customer",
    },
  };
}

afterEach(() => {
  clearAllAccessTokenCache();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("wecom-kf dispatch", () => {
  it("does not send outbound messages when the reply pipeline yields no visible payload", async () => {
    const fetchMock = vi.fn();
    const dispatchReplyWithBufferedBlockDispatcher = vi.fn(async () => undefined);
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const cfg: PluginConfig = {
      channels: {
        "wecom-kf": {
          enabled: true,
        },
      },
    };

    await dispatchKfMessage({
      cfg,
      account: createAccount(),
      msg: createTextMessage(),
      runtime: createRuntime(dispatchReplyWithBufferedBlockDispatcher),
    });

    expect(dispatchReplyWithBufferedBlockDispatcher).toHaveBeenCalledTimes(1);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
