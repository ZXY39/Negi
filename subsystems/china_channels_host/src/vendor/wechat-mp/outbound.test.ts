import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  resolveWechatMpAccount: vi.fn(),
  sendWechatMpActiveText: vi.fn(),
}));

vi.mock("./config.js", () => ({
  resolveWechatMpAccount: mocks.resolveWechatMpAccount,
}));

vi.mock("./send.js", () => ({
  sendWechatMpActiveText: mocks.sendWechatMpActiveText,
}));

import { wechatMpOutbound } from "./outbound.js";
import type { PluginConfig, ResolvedWechatMpAccount } from "./types.js";

function createAccount(overrides?: Partial<ResolvedWechatMpAccount>): ResolvedWechatMpAccount {
  return {
    accountId: "default",
    name: "WeChat MP (default)",
    enabled: true,
    configured: true,
    canSendActive: true,
    config: {
      appId: "wx-test-appid",
      appSecret: "secret",
      token: "token",
      encodingAESKey: "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
      webhookPath: "/wechat-mp",
      messageMode: "safe",
      replyMode: "passive",
    },
    ...overrides,
  };
}

describe("wechatMpOutbound sendText", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.resolveWechatMpAccount.mockReturnValue(createAccount());
    mocks.sendWechatMpActiveText.mockResolvedValue({ ok: true, msgid: "msg-1" });
  });

  it("normalizes markdown text by default", async () => {
    const result = await wechatMpOutbound.sendText({
      cfg: {} as PluginConfig,
      to: "user:openid-123",
      text: "**bold** and `code`",
    });

    expect(result.ok).toBe(true);
    expect(result.channel).toBe("wechat-mp");
    expect(mocks.sendWechatMpActiveText).toHaveBeenCalledWith(
      expect.objectContaining({
        toUserName: "openid-123",
        text: "bold and code",
      })
    );
  });

  it("preserves markdown when renderMarkdown is false", async () => {
    mocks.resolveWechatMpAccount.mockReturnValue(
      createAccount({
        config: {
          appId: "wx-test-appid",
          appSecret: "secret",
          token: "token",
          encodingAESKey: "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
          webhookPath: "/wechat-mp",
          messageMode: "safe",
          replyMode: "passive",
          renderMarkdown: false,
        },
      })
    );

    const result = await wechatMpOutbound.sendText({
      cfg: {} as PluginConfig,
      to: "user:openid-123",
      text: "**bold** and `code`",
    });

    expect(result.ok).toBe(true);
    expect(mocks.sendWechatMpActiveText).toHaveBeenCalledWith(
      expect.objectContaining({
        toUserName: "openid-123",
        text: "**bold** and `code`",
      })
    );
  });

  it("normalizes headings to bracketed format", async () => {
    const result = await wechatMpOutbound.sendText({
      cfg: {} as PluginConfig,
      to: "user:openid-123",
      text: "# Main Title\n## Subtitle",
    });

    expect(result.ok).toBe(true);
    expect(mocks.sendWechatMpActiveText).toHaveBeenCalledWith(
      expect.objectContaining({
        toUserName: "openid-123",
        text: "[Main Title]\n[Subtitle]",
      })
    );
  });

  it("parses user prefix with account suffix", async () => {
    mocks.resolveWechatMpAccount.mockReturnValue(
      createAccount({
        accountId: "account-2",
        config: {
          appId: "wx-app-2",
          appSecret: "secret-2",
          token: "token-2",
          webhookPath: "/wechat-mp-2",
          messageMode: "safe",
          replyMode: "active",
        },
      })
    );

    const result = await wechatMpOutbound.sendText({
      cfg: {} as PluginConfig,
      accountId: "account-2",
      to: "user:openid-456@account-2",
      text: "hello",
    });

    expect(result.ok).toBe(true);
    expect(mocks.resolveWechatMpAccount).toHaveBeenCalledWith(
      expect.objectContaining({
        accountId: "account-2",
      })
    );
    expect(mocks.sendWechatMpActiveText).toHaveBeenCalledWith(
      expect.objectContaining({
        toUserName: "openid-456",
        text: "hello",
      })
    );
  });

  it("parses wechat-mp prefix", async () => {
    const result = await wechatMpOutbound.sendText({
      cfg: {} as PluginConfig,
      to: "wechat-mp:user:openid-789",
      text: "test message",
    });

    expect(result.ok).toBe(true);
    expect(mocks.sendWechatMpActiveText).toHaveBeenCalledWith(
      expect.objectContaining({
        toUserName: "openid-789",
        text: "test message",
      })
    );
  });

  it("returns error for unsupported target format", async () => {
    const result = await wechatMpOutbound.sendText({
      cfg: {} as PluginConfig,
      to: "", // Empty target is invalid
      text: "hello",
    });

    expect(result.ok).toBe(false);
    expect(result.error).toBeInstanceOf(Error);
    expect(result.error?.message).toContain("Unsupported target");
    expect(mocks.sendWechatMpActiveText).not.toHaveBeenCalled();
  });

  it("returns error when send fails", async () => {
    mocks.sendWechatMpActiveText.mockResolvedValue({
      ok: false,
      error: "API error",
    });

    const result = await wechatMpOutbound.sendText({
      cfg: {} as PluginConfig,
      to: "user:openid-123",
      text: "hello",
    });

    expect(result.ok).toBe(false);
    expect(result.error).toBeInstanceOf(Error);
    expect(result.error?.message).toBe("API error");
  });

  it("applies normalization parity with reply path", async () => {
    // This test verifies that outbound and reply path have the same normalization behavior
    const markdownInput = `# Heading
**bold** and *italic*
- list item
\`inline code\``;

    const result = await wechatMpOutbound.sendText({
      cfg: {} as PluginConfig,
      to: "user:openid-123",
      text: markdownInput,
    });

    expect(result.ok).toBe(true);
    expect(mocks.sendWechatMpActiveText).toHaveBeenCalledWith(
      expect.objectContaining({
        toUserName: "openid-123",
        text: expect.stringContaining("[Heading]"),
      })
    );
    expect(mocks.sendWechatMpActiveText).toHaveBeenCalledWith(
      expect.objectContaining({
        text: expect.not.stringContaining("**bold**"),
      })
    );
    expect(mocks.sendWechatMpActiveText).toHaveBeenCalledWith(
      expect.objectContaining({
        text: expect.stringContaining("- list item"),
      })
    );
  });
});
