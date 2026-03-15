import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearQQBotRuntime, setQQBotRuntime } from "./runtime.js";

const outboundMocks = vi.hoisted(() => ({
  sendTyping: vi.fn(),
  sendText: vi.fn(),
  sendMedia: vi.fn(),
}));

const proactiveMocks = vi.hoisted(() => ({
  getKnownQQBotTarget: vi.fn(),
  upsertKnownQQBotTarget: vi.fn(),
}));

const refIndexMocks = vi.hoisted(() => ({
  getRefIndex: vi.fn(),
  setRefIndex: vi.fn(),
  formatRefEntryForAgent: vi.fn(),
}));

vi.mock("./outbound.js", () => ({
  qqbotOutbound: {
    sendTyping: outboundMocks.sendTyping,
    sendText: outboundMocks.sendText,
    sendMedia: outboundMocks.sendMedia,
  },
}));

vi.mock("./proactive.js", () => ({
  getKnownQQBotTarget: proactiveMocks.getKnownQQBotTarget,
  upsertKnownQQBotTarget: proactiveMocks.upsertKnownQQBotTarget,
}));

vi.mock("./ref-index-store.js", () => ({
  getRefIndex: refIndexMocks.getRefIndex,
  setRefIndex: refIndexMocks.setRefIndex,
  formatRefEntryForAgent: refIndexMocks.formatRefEntryForAgent,
}));

import { handleQQBotDispatch } from "./bot.js";

function createLogger() {
  return {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
  };
}

function installRuntime(params?: {
  dispatchReplyWithBufferedBlockDispatcher?: ReturnType<typeof vi.fn>;
}) {
  const dispatchReplyWithBufferedBlockDispatcher =
    params?.dispatchReplyWithBufferedBlockDispatcher ?? vi.fn().mockResolvedValue(undefined);

  setQQBotRuntime({
    channel: {
      routing: {
        resolveAgentRoute: () => ({
          sessionKey: "session-1",
          accountId: "default",
          agentId: "main",
        }),
      },
      reply: {
        dispatchReplyWithBufferedBlockDispatcher,
      },
      session: {
        resolveStorePath: () => "memory://qqbot",
        readSessionUpdatedAt: () => null,
        recordInboundSession: vi.fn().mockResolvedValue(undefined),
      },
    },
  });

  return {
    dispatchReplyWithBufferedBlockDispatcher,
  };
}

const baseCfg = {
  channels: {
    qqbot: {
      enabled: true,
      appId: "app-1",
      clientSecret: "secret-1",
      markdownSupport: true,
    },
  },
};

describe("QQBot ref-index quote context", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    proactiveMocks.getKnownQQBotTarget.mockReturnValue(undefined);
    outboundMocks.sendTyping.mockResolvedValue({ channel: "qqbot", refIdx: "REFIDX-typing-1" });
    outboundMocks.sendText.mockResolvedValue({ channel: "qqbot", messageId: "text-1", timestamp: 1 });
    outboundMocks.sendMedia.mockResolvedValue({ channel: "qqbot", messageId: "media-1", timestamp: 2 });
  });

  afterEach(() => {
    clearQQBotRuntime();
  });

  it("injects quoted body into BodyForAgent and stores the current msg_idx", async () => {
    refIndexMocks.getRefIndex.mockReturnValue({
      content: "上一条消息",
      senderId: "bot-1",
      senderName: "Bot One",
      timestamp: 1700000000000,
    });
    refIndexMocks.formatRefEntryForAgent.mockReturnValue("上一条消息\n[图片: evidence.png]");
    const runtime = installRuntime();
    const logger = createLogger();

    await handleQQBotDispatch({
      eventType: "C2C_MESSAGE_CREATE",
      eventData: {
        id: "msg-ref-1",
        event_id: "evt-ref-1",
        content: "现在这条消息",
        timestamp: 1700000001000,
        message_scene: {
          ext: ["ref_msg_idx=REFIDX-prev-1", "msg_idx=REFIDX-current-1"],
        },
        author: {
          user_openid: "u-ref-1",
          username: "Alice",
        },
      },
      cfg: baseCfg,
      accountId: "default",
      logger,
    });

    const ctx = runtime.dispatchReplyWithBufferedBlockDispatcher.mock.calls[0]?.[0]?.ctx ?? {};
    expect(ctx.ReplyToId).toBe("REFIDX-prev-1");
    expect(ctx.ReplyToBody).toBe("上一条消息\n[图片: evidence.png]");
    expect(ctx.ReplyToSender).toBe("Bot One");
    expect(ctx.ReplyToIsQuote).toBe(true);
    expect(ctx.BodyForAgent).toContain("[引用消息开始]");
    expect(ctx.BodyForAgent).toContain("上一条消息\n[图片: evidence.png]");
    expect(ctx.BodyForAgent).toContain("[引用消息结束]");
    expect(ctx.BodyForAgent).toContain("现在这条消息");
    expect(refIndexMocks.setRefIndex).toHaveBeenCalledWith(
      "REFIDX-current-1",
      expect.objectContaining({
        content: "现在这条消息",
        senderId: "u-ref-1",
        timestamp: 1700000001000,
      })
    );
  });

  it("falls back to typing refIdx when the inbound event does not include msg_idx", async () => {
    installRuntime();
    const logger = createLogger();

    await handleQQBotDispatch({
      eventType: "C2C_MESSAGE_CREATE",
      eventData: {
        id: "msg-ref-2",
        event_id: "evt-ref-2",
        content: "缺少 msg_idx",
        timestamp: 1700000002000,
        author: {
          user_openid: "u-ref-2",
          username: "Bob",
        },
      },
      cfg: baseCfg,
      accountId: "default",
      logger,
    });

    expect(refIndexMocks.setRefIndex).toHaveBeenCalledWith(
      "REFIDX-typing-1",
      expect.objectContaining({
        content: "缺少 msg_idx",
        senderId: "u-ref-2",
      })
    );
  });

  it("stores the resolved alias name in ref-index entries", async () => {
    installRuntime();
    const logger = createLogger();

    await handleQQBotDispatch({
      eventType: "C2C_MESSAGE_CREATE",
      eventData: {
        id: "msg-ref-alias-1",
        event_id: "evt-ref-alias-1",
        content: "别名引用缓存",
        timestamp: 1700000002050,
        author: {
          user_openid: "u-ref-alias-1",
          username: "Payload Name",
        },
      },
      cfg: {
        channels: {
          qqbot: {
            ...baseCfg.channels.qqbot,
            displayAliases: {
              "user:u-ref-alias-1": "Resolved Alias",
            },
          },
        },
      },
      accountId: "default",
      logger,
    });

    expect(refIndexMocks.setRefIndex).toHaveBeenCalledWith(
      "REFIDX-typing-1",
      expect.objectContaining({
        content: "别名引用缓存",
        senderId: "u-ref-alias-1",
        senderName: "Resolved Alias",
      })
    );
  });

  it("stores both msg_idx and typing refIdx when both are present", async () => {
    installRuntime();
    const logger = createLogger();

    await handleQQBotDispatch({
      eventType: "C2C_MESSAGE_CREATE",
      eventData: {
        id: "msg-ref-both-1",
        event_id: "evt-ref-both-1",
        content: "双索引缓存",
        timestamp: 1700000002500,
        message_scene: {
          ext: ["msg_idx=REFIDX-scene-1"],
        },
        author: {
          user_openid: "u-ref-both-1",
          username: "Both",
        },
      },
      cfg: baseCfg,
      accountId: "default",
      logger,
    });

    expect(refIndexMocks.setRefIndex).toHaveBeenNthCalledWith(
      1,
      "REFIDX-scene-1",
      expect.objectContaining({
        content: "双索引缓存",
      })
    );
    expect(refIndexMocks.setRefIndex).toHaveBeenNthCalledWith(
      2,
      "REFIDX-typing-1",
      expect.objectContaining({
        content: "双索引缓存",
      })
    );
  });

  it("uses a placeholder quote body when ref-index lookup misses", async () => {
    refIndexMocks.getRefIndex.mockReturnValue(null);
    const runtime = installRuntime();
    const logger = createLogger();

    await handleQQBotDispatch({
      eventType: "C2C_MESSAGE_CREATE",
      eventData: {
        id: "msg-ref-3",
        event_id: "evt-ref-3",
        content: "引用丢失",
        timestamp: 1700000003000,
        message_scene: {
          ext: ["ref_msg_idx=REFIDX-missing-1", "msg_idx=REFIDX-current-3"],
        },
        author: {
          user_openid: "u-ref-3",
          username: "Carol",
        },
      },
      cfg: baseCfg,
      accountId: "default",
      logger,
    });

    const ctx = runtime.dispatchReplyWithBufferedBlockDispatcher.mock.calls[0]?.[0]?.ctx ?? {};
    expect(ctx.ReplyToId).toBe("REFIDX-missing-1");
    expect(ctx.ReplyToBody).toBe("原始内容不可用");
    expect(ctx.ReplyToIsQuote).toBe(true);
    expect(ctx.BodyForAgent).not.toContain("[引用消息开始]");
    expect(ctx.BodyForAgent).not.toContain("原始内容不可用");
    expect(refIndexMocks.formatRefEntryForAgent).not.toHaveBeenCalled();
  });

  it("does not inject quote context into BodyForAgent for slash commands", async () => {
    refIndexMocks.getRefIndex.mockReturnValue({
      content: "需要忽略的引用",
      senderId: "bot-2",
      senderName: "Bot Two",
      timestamp: 1700000000000,
    });
    refIndexMocks.formatRefEntryForAgent.mockReturnValue("需要忽略的引用");
    const runtime = installRuntime();
    const logger = createLogger();

    await handleQQBotDispatch({
      eventType: "C2C_MESSAGE_CREATE",
      eventData: {
        id: "msg-ref-4",
        event_id: "evt-ref-4",
        content: "/quote status",
        timestamp: 1700000004000,
        message_scene: {
          ext: ["ref_msg_idx=REFIDX-prev-4", "msg_idx=REFIDX-current-4"],
        },
        author: {
          user_openid: "u-ref-4",
          username: "Dave",
        },
      },
      cfg: baseCfg,
      accountId: "default",
      logger,
    });

    const ctx = runtime.dispatchReplyWithBufferedBlockDispatcher.mock.calls[0]?.[0]?.ctx ?? {};
    expect(ctx.ReplyToId).toBe("REFIDX-prev-4");
    expect(ctx.ReplyToBody).toBe("需要忽略的引用");
    expect(ctx.ReplyToIsQuote).toBe(true);
    expect(ctx.BodyForAgent).toBeUndefined();
  });
});
