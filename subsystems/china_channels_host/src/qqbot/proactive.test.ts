import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const outboundMocks = vi.hoisted(() => ({
  sendText: vi.fn(),
  sendMedia: vi.fn(),
}));

vi.mock("./outbound.js", () => ({
  qqbotOutbound: {
    sendText: outboundMocks.sendText,
    sendMedia: outboundMocks.sendMedia,
  },
}));

import {
  clearKnownQQBotTargets,
  getKnownQQBotTarget,
  listKnownQQBotTargets,
  removeKnownQQBotTarget,
  sendProactiveQQBotMessage,
  upsertKnownQQBotTarget,
} from "./proactive.js";

function createStoreFilePath(): { dirPath: string; filePath: string } {
  const dirPath = mkdtempSync(join(tmpdir(), "qqbot-known-targets-"));
  return {
    dirPath,
    filePath: join(dirPath, "known-targets.json"),
  };
}

describe("QQBot known target registry", () => {
  let tempDirPath = "";
  let tempFilePath = "";

  beforeEach(() => {
    const tempPaths = createStoreFilePath();
    tempDirPath = tempPaths.dirPath;
    tempFilePath = tempPaths.filePath;
    vi.clearAllMocks();
  });

  afterEach(() => {
    if (tempDirPath) {
      rmSync(tempDirPath, { recursive: true, force: true });
    }
  });

  it("supports CRUD, dedupe, lastSeenAt updates, and persisted reload", () => {
    const created = upsertKnownQQBotTarget({
      filePath: tempFilePath,
      target: {
        accountId: "bot-a",
        kind: "user",
        target: "user:u-1",
        displayName: "Alice",
        sourceChatType: "direct",
        firstSeenAt: 100,
        lastSeenAt: 100,
      },
    });

    expect(created).toMatchObject({
      accountId: "bot-a",
      kind: "user",
      target: "user:u-1",
      displayName: "Alice",
      firstSeenAt: 100,
      lastSeenAt: 100,
    });
    expect(listKnownQQBotTargets({ filePath: tempFilePath })).toHaveLength(1);

    upsertKnownQQBotTarget({
      filePath: tempFilePath,
      target: {
        accountId: "bot-a",
        kind: "user",
        target: "user:u-1",
        sourceChatType: "direct",
        firstSeenAt: 999,
        lastSeenAt: 300,
      },
    });
    upsertKnownQQBotTarget({
      filePath: tempFilePath,
      target: {
        accountId: "bot-a",
        kind: "group",
        target: "group:g-1",
        displayName: "Team Group",
        sourceChatType: "group",
        firstSeenAt: 200,
        lastSeenAt: 200,
      },
    });

    const reloaded = listKnownQQBotTargets({ filePath: tempFilePath });
    expect(reloaded).toHaveLength(2);
    expect(reloaded[0]).toMatchObject({
      accountId: "bot-a",
      kind: "user",
      target: "user:u-1",
      displayName: "Alice",
      firstSeenAt: 100,
      lastSeenAt: 300,
    });
    expect(reloaded[1]).toMatchObject({
      accountId: "bot-a",
      kind: "group",
      target: "group:g-1",
      displayName: "Team Group",
      firstSeenAt: 200,
      lastSeenAt: 200,
    });
    expect(getKnownQQBotTarget({ filePath: tempFilePath, accountId: "bot-a", target: "user:u-1" })).toMatchObject({
      accountId: "bot-a",
      target: "user:u-1",
      lastSeenAt: 300,
    });
    expect(readFileSync(tempFilePath, "utf8")).toContain("\"target\": \"user:u-1\"");
    expect(readFileSync(tempFilePath, "utf8")).toContain("\"target\": \"group:g-1\"");

    expect(removeKnownQQBotTarget({ filePath: tempFilePath, accountId: "bot-a", target: "group:g-1" })).toBe(true);
    expect(listKnownQQBotTargets({ filePath: tempFilePath })).toHaveLength(1);
  });

  it("clears matching targets and removes the store file when empty", () => {
    upsertKnownQQBotTarget({
      filePath: tempFilePath,
      target: {
        accountId: "bot-a",
        kind: "user",
        target: "user:u-1",
        sourceChatType: "direct",
        firstSeenAt: 100,
        lastSeenAt: 100,
      },
    });
    upsertKnownQQBotTarget({
      filePath: tempFilePath,
      target: {
        accountId: "bot-a",
        kind: "group",
        target: "group:g-1",
        sourceChatType: "group",
        firstSeenAt: 200,
        lastSeenAt: 200,
      },
    });

    expect(clearKnownQQBotTargets({ filePath: tempFilePath, accountId: "bot-a" })).toBe(2);
    expect(listKnownQQBotTargets({ filePath: tempFilePath })).toEqual([]);
    expect(existsSync(tempFilePath)).toBe(false);
  });
});

describe("sendProactiveQQBotMessage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("delegates text sends to qqbotOutbound.sendText", async () => {
    outboundMocks.sendText.mockResolvedValue({
      channel: "qqbot",
      messageId: "text-1",
      timestamp: 1,
    });

    const result = await sendProactiveQQBotMessage({
      cfg: {
        channels: {
          qqbot: {
            appId: "app-1",
            clientSecret: "secret-1",
          },
        },
      },
      to: "user:u-1",
      text: "hello proactive",
      accountId: "bot-a",
    });

    expect(result).toEqual({
      channel: "qqbot",
      messageId: "text-1",
      timestamp: 1,
    });
    expect(outboundMocks.sendText).toHaveBeenCalledWith({
      cfg: {
        channels: {
          qqbot: {
            appId: "app-1",
            clientSecret: "secret-1",
          },
        },
      },
      to: "user:u-1",
      text: "hello proactive",
      accountId: "bot-a",
    });
    expect(outboundMocks.sendMedia).not.toHaveBeenCalled();
  });

  it("delegates media sends to qqbotOutbound.sendMedia", async () => {
    outboundMocks.sendMedia.mockResolvedValue({
      channel: "qqbot",
      messageId: "media-1",
      timestamp: 2,
    });

    const result = await sendProactiveQQBotMessage({
      cfg: {
        channels: {
          qqbot: {
            appId: "app-1",
            clientSecret: "secret-1",
          },
        },
      },
      to: "group:g-1",
      text: "附件说明",
      mediaUrl: "https://example.com/report.png",
    });

    expect(result).toEqual({
      channel: "qqbot",
      messageId: "media-1",
      timestamp: 2,
    });
    expect(outboundMocks.sendMedia).toHaveBeenCalledWith({
      cfg: {
        channels: {
          qqbot: {
            appId: "app-1",
            clientSecret: "secret-1",
          },
        },
      },
      to: "group:g-1",
      mediaUrl: "https://example.com/report.png",
      text: "附件说明",
      accountId: undefined,
    });
  });

  it("surfaces outbound missing-credential errors without changing the send path", async () => {
    outboundMocks.sendText.mockResolvedValue({
      channel: "qqbot",
      error: "QQBot not configured (missing appId/clientSecret)",
    });

    const result = await sendProactiveQQBotMessage({
      cfg: { channels: { qqbot: {} } },
      to: "user:u-1",
      text: "hello",
    });

    expect(result).toEqual({
      channel: "qqbot",
      error: "QQBot not configured (missing appId/clientSecret)",
    });
    expect(outboundMocks.sendText).toHaveBeenCalledTimes(1);
  });

  it("rejects empty proactive targets", async () => {
    const result = await sendProactiveQQBotMessage({
      cfg: { channels: { qqbot: {} } },
      to: "   ",
      text: "hello",
    });

    expect(result).toEqual({
      channel: "qqbot",
      error: "to is required for proactive send",
    });
    expect(outboundMocks.sendText).not.toHaveBeenCalled();
    expect(outboundMocks.sendMedia).not.toHaveBeenCalled();
  });

  it("rejects requests without text or mediaUrl", async () => {
    const result = await sendProactiveQQBotMessage({
      cfg: { channels: { qqbot: {} } },
      to: "user:u-1",
      text: "   ",
    });

    expect(result).toEqual({
      channel: "qqbot",
      error: "text or mediaUrl is required for proactive send",
    });
    expect(outboundMocks.sendText).not.toHaveBeenCalled();
    expect(outboundMocks.sendMedia).not.toHaveBeenCalled();
  });
});
