// @ts-nocheck
import http, { type IncomingMessage, type ServerResponse } from "node:http";
import { once } from "node:events";

import { listRegisteredChannels } from "./channel_registry.js";
import { type ChinaHostConfig } from "./config.js";
import { createLogger } from "./logger.js";
import { G3kuRuntimeBridge } from "./runtime_bridge.js";

type RouteHandler = {
  path: string;
  match: "exact" | "prefix";
  handler: (req: IncomingMessage, res: ServerResponse) => Promise<boolean> | boolean;
};

type ChannelPlugin = {
  config: {
    listAccountIds: (cfg: Record<string, unknown>) => string[];
    resolveAccount: (cfg: Record<string, unknown>, accountId: string) => unknown;
  };
  gateway: {
    startAccount: (params: {
      cfg: Record<string, unknown>;
      accountId: string;
      account: unknown;
      runtime: Record<string, unknown>;
      channelRuntime: Record<string, unknown>;
      abortSignal: AbortSignal;
      log: unknown;
      getStatus: () => Record<string, unknown>;
      setStatus: (next: Record<string, unknown>) => void;
    }) => Promise<unknown> | unknown;
  };
};

type PluginLoader = () => Promise<ChannelPlugin>;
type RouteHandlerLoader = () => Promise<RouteHandler["handler"]>;

function normalizeRoutePath(pathValue: string | undefined, fallback: string): string {
  const trimmed = pathValue?.trim() ?? "";
  const candidate = trimmed || fallback;
  return candidate.startsWith("/") ? candidate : `/${candidate}`;
}

function collectRoutePaths(config: any, fallback: string): string[] {
  const routes = new Set<string>([normalizeRoutePath(config?.webhookPath, fallback)]);
  for (const accountConfig of Object.values(config?.accounts ?? {})) {
    const customPath = (accountConfig as any)?.webhookPath?.trim();
    if (!customPath) continue;
    routes.add(normalizeRoutePath(customPath, fallback));
  }
  return [...routes];
}

function collectWecomRoutePaths(config: any): string[] {
  const routes = new Set<string>(["/wecom-media"]);
  if ((config?.mode ?? "ws") !== "ws") {
    routes.add(normalizeRoutePath(config?.webhookPath, "/wecom"));
  }
  for (const accountConfig of Object.values(config?.accounts ?? {})) {
    const accountMode = (accountConfig as any)?.mode ?? config?.mode ?? "ws";
    if (accountMode === "ws") continue;
    const customPath = (accountConfig as any)?.webhookPath?.trim();
    routes.add(normalizeRoutePath(customPath, "/wecom"));
  }
  return [...routes];
}

const CHANNEL_PLUGIN_LOADERS: Record<string, PluginLoader | undefined> = {
  qqbot: async () => (await import("./qqbot/channel.js")).qqbotPlugin,
  dingtalk: async () => (await import("./dingtalk/channel.js")).dingtalkPlugin,
  wecom: async () => (await import("./wecom/channel.js")).wecomPlugin,
  "wecom-app": async () => (await import("./wecom-app/channel.js")).wecomAppPlugin,
  "wecom-kf": async () => (await import("./wecom-kf/channel.js")).wecomKfPlugin,
  "wechat-mp": async () => (await import("./wechat-mp/channel.js")).wechatMpPlugin,
  "feishu-china": async () => (await import("./feishu-china/channel.js")).feishuPlugin,
};

const CHANNEL_ROUTE_MAP: Record<
  string,
  { collectPaths: (config: any) => string[]; loadHandler: RouteHandlerLoader } | undefined
> = {
  wecom: {
    collectPaths: collectWecomRoutePaths,
    loadHandler: async () => (await import("./wecom/monitor.js")).handleWecomWebhookRequest,
  },
  "wecom-app": {
    collectPaths: (config) => collectRoutePaths(config, "/wecom-app"),
    loadHandler: async () => (await import("./wecom-app/monitor.js")).handleWecomAppWebhookRequest,
  },
  "wecom-kf": {
    collectPaths: (config) => collectRoutePaths(config, "/wecom-kf"),
    loadHandler: async () => (await import("./wecom-kf/webhook.js")).handleWecomKfWebhookRequest,
  },
  "wechat-mp": {
    collectPaths: (config) => collectRoutePaths(config, "/wechat-mp"),
    loadHandler: async () => (await import("./wechat-mp/webhook.js")).handleWechatMpWebhookRequest,
  },
};

export class ChinaChannelsHost {
  private readonly logger = createLogger("host");
  private readonly routes: RouteHandler[] = [];
  private readonly statuses = new Map<string, Record<string, unknown>>();
  private readonly aborts = new Map<string, AbortController>();
  private readonly tasks = new Map<string, Promise<unknown>>();
  private publicServer: http.Server | null = null;
  private readonly runtimeBridge: G3kuRuntimeBridge;

  constructor(private readonly cfg: ChinaHostConfig) {
    this.runtimeBridge = new G3kuRuntimeBridge({
      host: String(cfg.chinaBridge.controlHost || "127.0.0.1"),
      port: Number(cfg.chinaBridge.controlPort || 18989),
      token: String(cfg.chinaBridge.controlToken || ""),
      version: "0.2.0",
      channelsConfig: cfg.channels,
    });
  }

  async start(): Promise<void> {
    await this.runtimeBridge.start();
    this.registerRoutes();
    this.publicServer = http.createServer((req, res) => this.handleHttp(req, res));
    this.publicServer.listen(
      Number(this.cfg.chinaBridge.publicPort || 18889),
      String(this.cfg.chinaBridge.bindHost || "0.0.0.0")
    );
    await once(this.publicServer, "listening");
    await this.startPlugins();
    this.logger.info(
      `public server listening on http://${this.cfg.chinaBridge.bindHost || "0.0.0.0"}:${this.cfg.chinaBridge.publicPort || 18889}`
    );
  }

  async stop(): Promise<void> {
    for (const abort of this.aborts.values()) {
      abort.abort();
    }
    this.aborts.clear();
    await Promise.allSettled(Array.from(this.tasks.values()));
    this.tasks.clear();
    if (this.publicServer) {
      await new Promise<void>((resolve) => this.publicServer?.close(() => resolve()));
      this.publicServer = null;
    }
    await this.runtimeBridge.stop();
  }

  private registerRoutes(): void {
    for (const item of listRegisteredChannels()) {
      const channelId = String(item.id || "").trim();
      const config = this.cfg.channels[channelId];
      const routeEntry = CHANNEL_ROUTE_MAP[channelId];
      if (!channelId || !routeEntry || !this.isEnabledChannelConfig(config)) continue;
      const loadHandler = routeEntry.loadHandler;
      let cachedHandler: RouteHandler["handler"] | null = null;
      for (const routePath of routeEntry.collectPaths(config)) {
        this.routes.push({
          path: routePath,
          match: "prefix",
          handler: async (req, res) => {
            if (!cachedHandler) {
              cachedHandler = await loadHandler();
            }
            return cachedHandler(req, res);
          },
        });
      }
    }
  }

  private async handleHttp(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const path = String(req.url || "/").split("?")[0] || "/";
    for (const route of this.routes) {
      const matched = route.match === "prefix" ? path.startsWith(route.path) : path === route.path;
      if (!matched) continue;
      const handled = await route.handler(req, res);
      if (handled) return;
    }
    res.statusCode = 404;
    res.end("not found");
  }

  private async startPlugins(): Promise<void> {
    const cfg = { channels: this.cfg.channels } as Record<string, unknown>;
    for (const item of listRegisteredChannels()) {
      const channelId = String(item.id || "").trim();
      const config = this.cfg.channels[channelId];
      const loadPlugin = CHANNEL_PLUGIN_LOADERS[channelId];
      if (!channelId || !loadPlugin || !this.isEnabledChannelConfig(config)) continue;
      const plugin = await loadPlugin();
      const accountIds = plugin.config.listAccountIds(cfg);
      for (const accountId of accountIds) {
        const account = plugin.config.resolveAccount(cfg, accountId);
        const abort = new AbortController();
        this.aborts.set(`${channelId}:${accountId}`, abort);
        const task = Promise.resolve(
          plugin.gateway.startAccount({
            cfg,
            accountId,
            account,
            runtime: this.runtimeBridge.runtime,
            channelRuntime: this.runtimeBridge.channelRuntime,
            abortSignal: abort.signal,
            log: this.logger,
            getStatus: () => this.statuses.get(`${channelId}:${accountId}`) ?? { accountId },
            setStatus: (next: Record<string, unknown>) => {
              this.statuses.set(`${channelId}:${accountId}`, next);
            },
          })
        ).catch((err) => {
          this.logger.error(`${channelId}:${accountId} gateway failed: ${String(err)}`);
        });
        this.tasks.set(`${channelId}:${accountId}`, task);
      }
    }
  }

  private isEnabledChannelConfig(config: any): boolean {
    return Boolean(config && config.enabled === true);
  }
}
