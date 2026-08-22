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

import { handleQQBotDispatch, isQQBotPauseCommandText } from "./bot.js";

function createLogger() {
  return {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
  };
}

function setupRuntime(params?: {
  routeResolver?: (input: {
    cfg: unknown;
    channel: string;
    accountId?: string;
    peer: { kind: string; id: string };
  }) => { sessionKey: string; accountId: string; agentId?: string };
  dispatchReplyWithBufferedBlockDispatcher?: ReturnType<typeof vi.fn>;
}) {
  const dispatchReplyWithBufferedBlockDispatcher =
    params?.dispatchReplyWithBufferedBlockDispatcher ?? vi.fn().mockResolvedValue(undefined);

  setQQBotRuntime({
    channel: {
      routing: {
        resolveAgentRoute:
          params?.routeResolver ??
          ((input) => ({
            sessionKey: `agent:main:qqbot:direct:${String(input.peer.id).toLowerCase()}`,
            accountId: input.accountId ?? "default",
            agentId: "main",
          })),
      },
      reply: {
        finalizeInboundContext: (ctx: unknown) => ctx,
        dispatchReplyWithBufferedBlockDispatcher,
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
    },
  },
};

describe("isQQBotPauseCommandText", () => {
  it("recognizes localized pause triggers and punctuation variants", () => {
    expect(isQQBotPauseCommandText("暂停")).toBe(true);
    expect(isQQBotPauseCommandText("暫停")).toBe(true);
    expect(isQQBotPauseCommandText("/pause")).toBe(true);
    expect(isQQBotPauseCommandText("Pause!")).toBe(true);
    expect(isQQBotPauseCommandText("暂停。")).toBe(true);
    expect(isQQBotPauseCommandText("  暂停  ")).toBe(true);
  });

  it("does not treat unrelated text as pause", () => {
    expect(isQQBotPauseCommandText("/pause on")).toBe(false);
    expect(isQQBotPauseCommandText("pause please")).toBe(false);
    expect(isQQBotPauseCommandText("停止")).toBe(false);
    expect(isQQBotPauseCommandText("继续")).toBe(false);
    expect(isQQBotPauseCommandText("")).toBe(false);
  });
});

describe("QQBot pause command queue handling", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    outboundMocks.sendTyping.mockResolvedValue({ channel: "qqbot" });
    outboundMocks.sendText.mockResolvedValue({ channel: "qqbot", messageId: "m-1", timestamp: 1 });
    outboundMocks.sendMedia.mockResolvedValue({ channel: "qqbot", messageId: "m-2", timestamp: 2 });
  });

  afterEach(() => {
    clearQQBotRuntime();
  });

  it("executes pause immediately while a dispatch is busy", async () => {
    const logger = createLogger();
    let releaseFirstDispatch: (() => void) | undefined;
    let resolveFirstEntered: (() => void) | undefined;
    let resolvePauseEntered: (() => void) | undefined;

    const firstEntered = new Promise<void>((resolve) => {
      resolveFirstEntered = resolve;
    });
    const pauseEntered = new Promise<void>((resolve) => {
      resolvePauseEntered = resolve;
    });
    const firstRelease = new Promise<void>((resolve) => {
      releaseFirstDispatch = resolve;
    });

    const dispatchReplyWithBufferedBlockDispatcher = vi.fn(async ({ ctx }: { ctx: Record<string, unknown> }) => {
      const rawBody = typeof ctx.RawBody === "string" ? ctx.RawBody : "";
      if (rawBody === "first") {
        resolveFirstEntered?.();
        await firstRelease;
        return;
      }
      if (rawBody === "暂停") {
        resolvePauseEntered?.();
      }
    });

    setupRuntime({
      routeResolver: (input) => ({
        sessionKey: "shared-session",
        accountId: input.accountId ?? "default",
        agentId: "main",
      }),
      dispatchReplyWithBufferedBlockDispatcher,
    });

    const firstDispatch = handleQQBotDispatch({
      eventType: "C2C_MESSAGE_CREATE",
      eventData: {
        id: "msg-pause-1",
        content: "first",
        timestamp: 1700000000500,
        author: {
          user_openid: "u-pause",
          username: "Pause User",
        },
      },
      cfg: baseCfg,
      accountId: "default",
      logger,
    });

    await firstEntered;

    const pauseDispatch = handleQQBotDispatch({
      eventType: "C2C_MESSAGE_CREATE",
      eventData: {
        id: "msg-pause-2",
        content: "暂停",
        timestamp: 1700000000600,
        author: {
          user_openid: "u-pause",
          username: "Pause User",
        },
      },
      cfg: baseCfg,
      accountId: "default",
      logger,
    });

    await pauseEntered;

    // 暂停绕过忙碌队列立即执行。
    expect(dispatchReplyWithBufferedBlockDispatcher).toHaveBeenCalledTimes(2);
    expect(logger.info).toHaveBeenCalledWith(
      expect.stringContaining("session pause command detected; executing immediately")
    );
    // 暂停保留排队消息：不产生 drop 日志。
    expect(logger.info).not.toHaveBeenCalledWith(
      expect.stringContaining("dropped")
    );

    releaseFirstDispatch?.();

    await Promise.all([firstDispatch, pauseDispatch]);

    expect(dispatchReplyWithBufferedBlockDispatcher).toHaveBeenCalledTimes(2);
  });

  it("suppresses stale reply payloads after pause and keeps the pause acknowledgement", async () => {
    const logger = createLogger();
    let releaseFirstDispatch: (() => void) | undefined;
    let resolveFirstEntered: (() => void) | undefined;
    let resolvePauseEntered: (() => void) | undefined;

    const firstEntered = new Promise<void>((resolve) => {
      resolveFirstEntered = resolve;
    });
    const pauseEntered = new Promise<void>((resolve) => {
      resolvePauseEntered = resolve;
    });
    const firstRelease = new Promise<void>((resolve) => {
      releaseFirstDispatch = resolve;
    });

    const dispatchReplyWithBufferedBlockDispatcher = vi.fn(
      async ({
        ctx,
        dispatcherOptions,
      }: {
        ctx: Record<string, unknown>;
        dispatcherOptions: {
          deliver: (payload: unknown, info?: { kind?: string }) => Promise<void>;
        };
      }) => {
        const rawBody = typeof ctx.RawBody === "string" ? ctx.RawBody : "";
        if (rawBody === "first") {
          resolveFirstEntered?.();
          await firstRelease;
          await dispatcherOptions.deliver({ text: "stale first reply" }, { kind: "final" });
          return;
        }
        if (rawBody === "暂停") {
          resolvePauseEntered?.();
          await dispatcherOptions.deliver({ text: "已暂停。" }, { kind: "final" });
        }
      }
    );

    setupRuntime({
      routeResolver: (input) => ({
        sessionKey: "shared-session",
        accountId: input.accountId ?? "default",
        agentId: "main",
      }),
      dispatchReplyWithBufferedBlockDispatcher,
    });

    const firstDispatch = handleQQBotDispatch({
      eventType: "C2C_MESSAGE_CREATE",
      eventData: {
        id: "msg-pause-suppress-1",
        content: "first",
        timestamp: 1700000000750,
        author: {
          user_openid: "u-pause",
          username: "Pause User",
        },
      },
      cfg: baseCfg,
      accountId: "default",
      logger,
    });

    await firstEntered;

    const pauseDispatch = handleQQBotDispatch({
      eventType: "C2C_MESSAGE_CREATE",
      eventData: {
        id: "msg-pause-suppress-2",
        content: "暂停",
        timestamp: 1700000000760,
        author: {
          user_openid: "u-pause",
          username: "Pause User",
        },
      },
      cfg: baseCfg,
      accountId: "default",
      logger,
    });

    await pauseEntered;
    releaseFirstDispatch?.();

    await Promise.all([firstDispatch, pauseDispatch]);

    // 被暂停回合的残留回复被 abort 代数抑制，仅暂停回执送达。
    expect(outboundMocks.sendText).toHaveBeenCalledTimes(1);
    expect(outboundMocks.sendText).toHaveBeenCalledWith(
      expect.objectContaining({
        text: "已暂停。",
      })
    );
  });
});
