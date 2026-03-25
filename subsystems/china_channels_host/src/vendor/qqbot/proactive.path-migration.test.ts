import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./outbound.js", () => ({
  qqbotOutbound: {
    sendText: vi.fn(),
    sendMedia: vi.fn(),
  },
}));

describe("QQBot known target path migration", () => {
  let tempHomePath = "";

  beforeEach(() => {
    tempHomePath = mkdtempSync(join(tmpdir(), "qqbot-known-targets-home-"));
    vi.resetModules();
  });

  afterEach(() => {
    vi.doUnmock("node:os");
    vi.resetModules();
    if (tempHomePath) {
      rmSync(tempHomePath, { recursive: true, force: true });
    }
  });

  it("migrates the legacy known-targets file into ~/.openclaw/qqbot/data", async () => {
    const legacyFilePath = join(tempHomePath, ".openclaw", "data", "qqbot", "known-targets.json");
    const nextFilePath = join(tempHomePath, ".openclaw", "qqbot", "data", "known-targets.json");

    mkdirSync(dirname(legacyFilePath), { recursive: true });
    writeFileSync(
      legacyFilePath,
      `${JSON.stringify(
        [
          {
            accountId: "dragon",
            kind: "user",
            target: "user:u-1",
            sourceChatType: "direct",
            firstSeenAt: 100,
            lastSeenAt: 200,
          },
        ],
        null,
        2
      )}\n`,
      "utf8"
    );

    vi.doMock("node:os", async () => {
      const actual = await vi.importActual<typeof import("node:os")>("node:os");
      return {
        ...actual,
        homedir: () => tempHomePath,
      };
    });

    const { listKnownQQBotTargets, upsertKnownQQBotTarget } = await import("./proactive.js");

    expect(listKnownQQBotTargets()).toEqual([
      {
        accountId: "dragon",
        kind: "user",
        target: "user:u-1",
        sourceChatType: "direct",
        firstSeenAt: 100,
        lastSeenAt: 200,
      },
    ]);
    expect(existsSync(nextFilePath)).toBe(true);
    expect(existsSync(legacyFilePath)).toBe(false);

    upsertKnownQQBotTarget({
      target: {
        accountId: "snake",
        kind: "user",
        target: "user:u-2",
        sourceChatType: "direct",
        firstSeenAt: 300,
        lastSeenAt: 300,
      },
    });

    const persisted = readFileSync(nextFilePath, "utf8");
    expect(persisted).toContain("\"accountId\": \"dragon\"");
    expect(persisted).toContain("\"accountId\": \"snake\"");
  });
});
