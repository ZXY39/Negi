import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const cancelMock = vi.fn();
const confirmMock = vi.fn();
const introMock = vi.fn();
const noteMock = vi.fn();
const outroMock = vi.fn();
const selectMock = vi.fn();
const textMock = vi.fn();

vi.mock("@clack/prompts", () => ({
  cancel: (...args: unknown[]) => cancelMock(...args),
  confirm: (...args: unknown[]) => confirmMock(...args),
  intro: (...args: unknown[]) => introMock(...args),
  isCancel: () => false,
  note: (...args: unknown[]) => noteMock(...args),
  outro: (...args: unknown[]) => outroMock(...args),
  select: (...args: unknown[]) => selectMock(...args),
  text: (...args: unknown[]) => textMock(...args),
}));

import { registerChinaSetupCli } from "./china-setup.js";

type ActionHandler = () => void | Promise<void>;

type LoggerLike = {
  info?: (message: string) => void;
  warn?: (message: string) => void;
  error?: (message: string) => void;
};

type CommandNode = {
  children: Map<string, CommandNode>;
  actionHandler?: ActionHandler;
  command: (name: string) => CommandNode;
  description: (text: string) => CommandNode;
  action: (handler: ActionHandler) => CommandNode;
};

type ConfigRoot = {
  channels?: Record<string, Record<string, unknown>>;
};

const CLI_STATE_KEY = Symbol.for("@openclaw-china/china-cli-state");

function createCommandNode(): CommandNode {
  const node: CommandNode = {
    children: new Map<string, CommandNode>(),
    command(name: string): CommandNode {
      const child = createCommandNode();
      node.children.set(name, child);
      return child;
    },
    description(): CommandNode {
      return node;
    },
    action(handler: ActionHandler): CommandNode {
      node.actionHandler = handler;
      return node;
    },
  };
  return node;
}

async function runSetup(initialConfig: ConfigRoot): Promise<{
  writeConfigFile: ReturnType<typeof vi.fn>;
  channels?: string[];
}> {
  let registrar:
    | ((ctx: { program: unknown; config?: unknown; logger?: LoggerLike }) => void | Promise<void>)
    | undefined;
  const writeConfigFile = vi.fn(async (_cfg: ConfigRoot) => {});

  registerChinaSetupCli(
    {
      runtime: {
        config: {
          writeConfigFile,
        },
      },
      registerCli: (nextRegistrar) => {
        registrar = nextRegistrar;
      },
    },
    { channels: ["wecom"] }
  );

  const program = createCommandNode();
  await registrar?.({
    program,
    config: initialConfig,
    logger: {},
  });

  const setupCommand = program.children.get("china")?.children.get("setup");
  expect(setupCommand?.actionHandler).toBeTypeOf("function");
  await setupCommand?.actionHandler?.();

  return { writeConfigFile };
}

describe("china setup wecom", () => {
  const stdinDescriptor = Object.getOwnPropertyDescriptor(process.stdin, "isTTY");
  const stdoutDescriptor = Object.getOwnPropertyDescriptor(process.stdout, "isTTY");

  beforeEach(() => {
    vi.clearAllMocks();
    delete (globalThis as Record<PropertyKey, unknown>)[CLI_STATE_KEY];
    Object.defineProperty(process.stdin, "isTTY", {
      configurable: true,
      value: true,
    });
    Object.defineProperty(process.stdout, "isTTY", {
      configurable: true,
      value: true,
    });
  });

  afterEach(() => {
    if (stdinDescriptor) {
      Object.defineProperty(process.stdin, "isTTY", stdinDescriptor);
    }
    if (stdoutDescriptor) {
      Object.defineProperty(process.stdout, "isTTY", stdoutDescriptor);
    }
  });

  it("stores ws-only credentials for wecom setup", async () => {
    selectMock.mockResolvedValueOnce("wecom");
    textMock.mockResolvedValueOnce("bot-123").mockResolvedValueOnce("secret-456");
    confirmMock.mockResolvedValueOnce(false);

    const { writeConfigFile } = await runSetup({
      channels: {
        wecom: {
          webhookPath: "/legacy-wecom",
          token: "legacy-token",
          encodingAESKey: "legacy-aes-key",
        },
      },
    });

    expect(writeConfigFile).toHaveBeenCalledTimes(1);
    const savedConfig = writeConfigFile.mock.calls[0]?.[0] as ConfigRoot;
    const wecomConfig = savedConfig.channels?.wecom;

    expect(wecomConfig?.enabled).toBe(true);
    expect(wecomConfig?.mode).toBe("ws");
    expect(wecomConfig?.botId).toBe("bot-123");
    expect(wecomConfig?.secret).toBe("secret-456");
    expect(wecomConfig?.webhookPath).toBeUndefined();
    expect(wecomConfig?.token).toBeUndefined();
    expect(wecomConfig?.encodingAESKey).toBeUndefined();

    const promptMessages = textMock.mock.calls.map((call) => {
      const firstArg = call[0] as { message?: string } | undefined;
      return firstArg?.message ?? "";
    });
    expect(promptMessages).toEqual(["WeCom botId（ws 长连接）", "WeCom secret（ws 长连接）"]);
  });

  it("marks wecom as configured when botId and secret already exist", async () => {
    let selectOptions: Array<{ label?: string; value?: string }> = [];
    selectMock.mockImplementationOnce(async (params: { options?: Array<{ label?: string; value?: string }> }) => {
      selectOptions = params.options ?? [];
      return "cancel";
    });

    const { writeConfigFile } = await runSetup({
      channels: {
        wecom: {
          botId: "existing-bot",
          secret: "existing-secret",
        },
      },
    });

    expect(writeConfigFile).not.toHaveBeenCalled();
    expect(selectOptions.some((option) => option.label === "WeCom（企业微信-智能机器人）（已配置）")).toBe(true);
  });
});

describe("china setup dingtalk", () => {
  const stdinDescriptor = Object.getOwnPropertyDescriptor(process.stdin, "isTTY");
  const stdoutDescriptor = Object.getOwnPropertyDescriptor(process.stdout, "isTTY");

  beforeEach(() => {
    vi.clearAllMocks();
    delete (globalThis as Record<PropertyKey, unknown>)[CLI_STATE_KEY];
    Object.defineProperty(process.stdin, "isTTY", {
      configurable: true,
      value: true,
    });
    Object.defineProperty(process.stdout, "isTTY", {
      configurable: true,
      value: true,
    });
  });

  afterEach(() => {
    if (stdinDescriptor) {
      Object.defineProperty(process.stdin, "isTTY", stdinDescriptor);
    }
    if (stdoutDescriptor) {
      Object.defineProperty(process.stdout, "isTTY", stdoutDescriptor);
    }
  });

  it("stores gateway token when dingtalk AI Card streaming is enabled", async () => {
    let registrar:
      | ((ctx: { program: unknown; config?: unknown; logger?: LoggerLike }) => void | Promise<void>)
      | undefined;
    const writeConfigFile = vi.fn(async (_cfg: ConfigRoot) => {});

    registerChinaSetupCli(
      {
        runtime: {
          config: {
            writeConfigFile,
          },
        },
        registerCli: (nextRegistrar) => {
          registrar = nextRegistrar;
        },
      },
      { channels: ["dingtalk"] }
    );

    selectMock.mockResolvedValueOnce("dingtalk");
    textMock.mockResolvedValueOnce("ding-app-key");
    textMock.mockResolvedValueOnce("ding-app-secret");
    confirmMock.mockResolvedValueOnce(true);
    textMock.mockResolvedValueOnce("gateway-token-123");

    const program = createCommandNode();
    await registrar?.({
      program,
      config: {
        gateway: {
          auth: {
            token: "global-token",
          },
        },
      },
      logger: {},
    });

    const setupCommand = program.children.get("china")?.children.get("setup");
    expect(setupCommand?.actionHandler).toBeTypeOf("function");
    await setupCommand?.actionHandler?.();

    expect(writeConfigFile).toHaveBeenCalledTimes(1);
    const savedConfig = writeConfigFile.mock.calls[0]?.[0] as ConfigRoot;
    const dingtalkConfig = savedConfig.channels?.dingtalk;

    expect(dingtalkConfig?.enabled).toBe(true);
    expect(dingtalkConfig?.clientId).toBe("ding-app-key");
    expect(dingtalkConfig?.clientSecret).toBe("ding-app-secret");
    expect(dingtalkConfig?.enableAICard).toBe(true);
    expect(dingtalkConfig?.gatewayToken).toBe("gateway-token-123");

    const promptMessages = textMock.mock.calls.map((call) => {
      const firstArg = call[0] as { message?: string } | undefined;
      return firstArg?.message ?? "";
    });
    expect(promptMessages).toContain(
      "OpenClaw Gateway Token（流式输出必需；留空则使用全局 gateway.auth.token）"
    );
  });
});
