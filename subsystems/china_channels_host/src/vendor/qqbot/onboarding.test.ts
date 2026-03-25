import { describe, expect, it, vi } from "vitest";
import { DEFAULT_ACCOUNT_ID } from "./config.js";
import { qqbotOnboardingAdapter } from "./onboarding.js";

function createPrompter() {
  return {
    note: vi.fn().mockResolvedValue(undefined),
    text: vi.fn(),
    confirm: vi.fn(),
    select: vi.fn(),
  };
}

describe("qqbotOnboardingAdapter.getStatus", () => {
  it("returns unconfigured status when credentials are missing", async () => {
    const status = await qqbotOnboardingAdapter.getStatus({ cfg: {} });

    expect(status).toEqual({
      channel: "qqbot",
      configured: false,
      statusLines: ["QQ Bot: 需要 AppID 和 ClientSecret"],
      selectionHint: "需要 AppID 和 ClientSecret",
      quickstartScore: 0,
    });
  });

  it("returns configured status for the default account", async () => {
    const status = await qqbotOnboardingAdapter.getStatus({
      cfg: {
        channels: {
          qqbot: {
            appId: "app-1",
            clientSecret: "secret-1",
          },
        },
      },
    });

    expect(status.configured).toBe(true);
    expect(status.statusLines).toEqual(["QQ Bot: 已配置"]);
    expect(status.selectionHint).toBe("已配置");
    expect(status.quickstartScore).toBe(2);
  });

  it("reports the configured account in multi-account mode", async () => {
    const status = await qqbotOnboardingAdapter.getStatus({
      cfg: {
        channels: {
          qqbot: {
            defaultAccount: "bot-b",
            accounts: {
              "bot-a": {
                enabled: true,
              },
              "bot-b": {
                enabled: true,
                appId: "app-b",
                clientSecret: "secret-b",
              },
            },
          },
        },
      },
    });

    expect(status.configured).toBe(true);
    expect(status.statusLines).toEqual(["QQ Bot: 已配置 (bot-b)"]);
  });
});

describe("qqbotOnboardingAdapter.configure", () => {
  it("keeps existing credentials when the user confirms reuse", async () => {
    const prompter = createPrompter();
    prompter.confirm.mockResolvedValue(true);

    const initialCfg = {
      channels: {
        qqbot: {
          enabled: true,
          appId: "app-1",
          clientSecret: "secret-1",
          markdownSupport: false,
        },
      },
    };

    const result = await qqbotOnboardingAdapter.configure({
      cfg: initialCfg,
      prompter,
    });

    expect(result).toEqual({
      cfg: initialCfg,
      accountId: DEFAULT_ACCOUNT_ID,
    });
    expect(prompter.note).not.toHaveBeenCalled();
    expect(prompter.text).not.toHaveBeenCalled();
  });

  it("prompts for a selected account and writes new credentials", async () => {
    const prompter = createPrompter();
    prompter.select.mockResolvedValue("bot-b");
    prompter.text
      .mockResolvedValueOnce("new-app-id")
      .mockResolvedValueOnce("new-client-secret");

    const result = await qqbotOnboardingAdapter.configure({
      cfg: {
        channels: {
          qqbot: {
            enabled: true,
            markdownSupport: false,
            accounts: {
              "bot-a": {
                enabled: true,
                appId: "app-a",
                clientSecret: "secret-a",
              },
              "bot-b": {
                enabled: true,
              },
            },
          },
        },
      },
      prompter,
      shouldPromptAccountIds: true,
    });

    expect(prompter.select).toHaveBeenCalledWith({
      message: "选择要配置的 QQ Bot 账户",
      options: [
        { value: "bot-a", label: "bot-a" },
        { value: "bot-b", label: "bot-b" },
      ],
      initialValue: "bot-a",
    });
    expect(prompter.note).toHaveBeenCalledTimes(1);
    expect(result.accountId).toBe("bot-b");
    expect(result.cfg.channels?.qqbot?.markdownSupport).toBe(false);
    expect(result.cfg.channels?.qqbot?.accounts?.["bot-b"]).toMatchObject({
      enabled: true,
      appId: "new-app-id",
      clientSecret: "new-client-secret",
    });
  });
});

describe("qqbotOnboardingAdapter.disable", () => {
  it("only flips qqbot.enabled to false", () => {
    const initialCfg = {
      channels: {
        qqbot: {
          enabled: true,
          appId: "app-1",
          clientSecret: "secret-1",
          markdownSupport: false,
          accounts: {
            sidecar: {
              enabled: true,
              appId: "side-app",
              clientSecret: "side-secret",
            },
          },
        },
      },
    };

    const disabled = qqbotOnboardingAdapter.disable(initialCfg);

    expect(disabled.channels?.qqbot).toMatchObject({
      enabled: false,
      appId: "app-1",
      clientSecret: "secret-1",
      markdownSupport: false,
      accounts: {
        sidecar: {
          enabled: true,
          appId: "side-app",
          clientSecret: "side-secret",
        },
      },
    });
  });
});
