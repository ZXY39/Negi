import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  MockWebSocket: class MockWebSocket {
    static OPEN = 1;
    static CLOSED = 3;
    static instances: InstanceType<typeof MockWebSocket>[] = [];

    readonly url: string;
    readyState = MockWebSocket.OPEN;
    private readonly listeners = new Map<string, Array<(...args: unknown[]) => void>>();

    constructor(url: string) {
      this.url = url;
      MockWebSocket.instances.push(this);
    }

    on(event: string, handler: (...args: unknown[]) => void): this {
      const handlers = this.listeners.get(event) ?? [];
      handlers.push(handler);
      this.listeners.set(event, handlers);
      return this;
    }

    send(): void {
      // no-op for monitor tests
    }

    emitMessage(payload: unknown): void {
      const body = typeof payload === "string" ? payload : JSON.stringify(payload);
      this.emit("message", body);
    }

    emitClose(code = 1000, reason = "closed"): void {
      if (this.readyState === MockWebSocket.CLOSED) return;
      this.readyState = MockWebSocket.CLOSED;
      this.emit("close", code, reason);
    }

    close(): void {
      this.emitClose();
    }

    static reset(): void {
      MockWebSocket.instances = [];
    }

    private emit(event: string, ...args: unknown[]): void {
      for (const handler of this.listeners.get(event) ?? []) {
        handler(...args);
      }
    }
  },
  clearTokenCache: vi.fn(),
  getAccessToken: vi.fn(),
  getGatewayUrl: vi.fn(),
  handleQQBotDispatch: vi.fn(),
  logger: {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
  },
}));

vi.mock("ws", () => ({
  default: mocks.MockWebSocket,
}));

vi.mock("./client.js", () => ({
  clearTokenCache: mocks.clearTokenCache,
  getAccessToken: mocks.getAccessToken,
  getGatewayUrl: mocks.getGatewayUrl,
}));

vi.mock("./bot.js", () => ({
  handleQQBotDispatch: mocks.handleQQBotDispatch,
}));

vi.mock("./logger.js", () => ({
  createLogger: () => mocks.logger,
}));

import {
  getActiveAccountIds,
  isQQBotMonitorActiveForAccount,
  monitorQQBotProvider,
  stopAllQQBotMonitors,
  stopQQBotMonitorForAccount,
} from "./monitor.js";

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function flushMicrotasks(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

const baseConfig = {
  channels: {
    qqbot: {
      appId: "app-1",
      clientSecret: "secret-1",
    },
  },
};

describe("QQBot monitor", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
    mocks.MockWebSocket.reset();
    mocks.getAccessToken.mockResolvedValue("token-1");
    mocks.getGatewayUrl.mockResolvedValue("wss://gateway.example/ws");
  });

  afterEach(() => {
    stopAllQQBotMonitors();
    vi.useRealTimers();
  });

  it("reports inactive when no connection exists", () => {
    expect(isQQBotMonitorActiveForAccount("missing")).toBe(false);
  });

  it("reuses the in-flight monitor start for duplicate account starts", async () => {
    const tokenDeferred = deferred<string>();
    mocks.getAccessToken.mockReturnValueOnce(tokenDeferred.promise);

    const first = monitorQQBotProvider({ config: baseConfig, accountId: "dragon" });
    await flushMicrotasks();
    const second = monitorQQBotProvider({ config: baseConfig, accountId: "dragon" });

    expect(getActiveAccountIds()).toEqual(["dragon"]);
    expect(mocks.MockWebSocket.instances).toHaveLength(0);

    tokenDeferred.resolve("token-1");
    await flushMicrotasks();

    expect(mocks.getGatewayUrl).toHaveBeenCalledTimes(1);
    expect(mocks.MockWebSocket.instances).toHaveLength(1);
    expect(isQQBotMonitorActiveForAccount("dragon")).toBe(true);

    stopQQBotMonitorForAccount("dragon");

    const completion = await Promise.race([
      Promise.allSettled([first, second]).then(() => "settled"),
      new Promise<string>((resolve) => setTimeout(() => resolve("timeout"), 50)),
    ]);

    expect(completion).toBe("settled");
    expect(isQQBotMonitorActiveForAccount("dragon")).toBe(false);
  });

  it("does not create a websocket after aborting an in-flight connect", async () => {
    const gatewayDeferred = deferred<string>();
    mocks.getGatewayUrl.mockReturnValueOnce(gatewayDeferred.promise);
    const controller = new AbortController();

    const running = monitorQQBotProvider({
      config: baseConfig,
      accountId: "snake",
      abortSignal: controller.signal,
    });
    await flushMicrotasks();

    controller.abort();
    await running;

    gatewayDeferred.resolve("wss://gateway.example/ws");
    await flushMicrotasks();

    expect(mocks.MockWebSocket.instances).toHaveLength(0);
    expect(getActiveAccountIds()).toEqual([]);
    expect(isQQBotMonitorActiveForAccount("snake")).toBe(false);
  });

  it("ignores stale socket events after reconnecting the same account", async () => {
    vi.useFakeTimers();

    const running = monitorQQBotProvider({ config: baseConfig, accountId: "phoenix" });
    await flushMicrotasks();

    expect(mocks.MockWebSocket.instances).toHaveLength(1);
    const firstSocket = mocks.MockWebSocket.instances[0];
    firstSocket?.emitMessage({ op: 7 });
    await vi.advanceTimersByTimeAsync(1000);
    await flushMicrotasks();

    expect(mocks.MockWebSocket.instances).toHaveLength(2);
    const secondSocket = mocks.MockWebSocket.instances[1];
    expect(secondSocket?.readyState).toBe(mocks.MockWebSocket.OPEN);

    firstSocket?.emitClose(1006, "stale-close");
    firstSocket?.emitMessage({ op: 10, d: { heartbeat_interval: 30000 } });
    await flushMicrotasks();

    expect(secondSocket?.readyState).toBe(mocks.MockWebSocket.OPEN);
    expect(mocks.getAccessToken).toHaveBeenCalledTimes(2);
    expect(mocks.getGatewayUrl).toHaveBeenCalledTimes(2);

    stopQQBotMonitorForAccount("phoenix");
    await running;
  });

  it("does not leave account entries behind when config validation fails", async () => {
    await expect(
      monitorQQBotProvider({
        config: { channels: { qqbot: {} } },
        accountId: "broken",
      })
    ).rejects.toThrow("missing appId or clientSecret");

    expect(getActiveAccountIds()).toEqual([]);
    expect(isQQBotMonitorActiveForAccount("broken")).toBe(false);
    expect(mocks.MockWebSocket.instances).toHaveLength(0);
  });
});
