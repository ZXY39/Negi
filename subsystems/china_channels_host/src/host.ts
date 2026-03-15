import http, { type IncomingMessage, type ServerResponse } from "node:http";
import { once } from "node:events";

import { dingtalkPlugin } from "./dingtalk/channel.js";
import { qqbotPlugin } from "./qqbot/channel.js";
import { wecomPlugin } from "./wecom/channel.js";
import { wecomAppPlugin } from "./wecom_app/channel.js";
import { feishuPlugin } from "./feishu_china/channel.js";
import { loadHostConfig, type ChinaHostConfig } from "./config.js";
import { createLogger } from "./logger.js";
import { handleWecomWebhookRequest } from "./wecom/monitor.js";
import { handleWecomAppWebhookRequest } from "./wecom_app/monitor.js";
import { G3kuRuntimeBridge } from "./runtime_bridge.js";

type RouteHandler = {
  path: string;
  match: "exact" | "prefix";
  handler: (req: IncomingMessage, res: ServerResponse) => Promise<boolean> | boolean;
};

function normalizeRoutePath(path: string | undefined, fallback: string): string {
  const trimmed = path?.trim() ?? "";
  const candidate = trimmed || fallback;
  return candidate.startsWith("/") ? candidate : `/${candidate}`;
}

function collectWecomRoutePaths(config: any): string[] {
  const routes = new Set<string>(["/wecom-media"]);
  if ((config?.mode ?? "ws") !== "ws") {
    routes.add(normalizeRoutePath(config?.webhookPath, "/wecom"));
  }
  for (const accountConfig of Object.values(config?.accounts ?? {})) {
    if ((accountConfig as any)?.mode ?? config?.mode ?? "ws" === "ws") continue;
    const customPath = (accountConfig as any)?.webhookPath?.trim();
    routes.add(normalizeRoutePath(customPath, "/wecom"));
  }
  return [...routes];
}

function collectWecomAppRoutePaths(config: any): string[] {
  const routes = new Set<string>([normalizeRoutePath(config?.webhookPath, "/wecom-app")]);
  for (const accountConfig of Object.values(config?.accounts ?? {})) {
    const customPath = (accountConfig as any)?.webhookPath?.trim();
    if (!customPath) continue;
    routes.add(normalizeRoutePath(customPath, "/wecom-app"));
  }
  return [...routes];
}

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
      version: "0.1.0",
      channelsConfig: cfg.channels,
    });
  }

  async start(): Promise<void> {
    await this.runtimeBridge.start();
    this.registerRoutes();
    this.publicServer = http.createServer((req, res) => this.handleHttp(req, res));
    this.publicServer.listen(Number(this.cfg.chinaBridge.publicPort || 18889), String(this.cfg.chinaBridge.bindHost || "0.0.0.0"));
    await once(this.publicServer, "listening");
    await this.startPlugins();
    this.logger.info(`public server listening on http://${this.cfg.chinaBridge.bindHost || "0.0.0.0"}:${this.cfg.chinaBridge.publicPort || 18889}`);
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
    for (const path of collectWecomRoutePaths(this.cfg.channels.wecom)) {
      this.routes.push({ path, match: "prefix", handler: handleWecomWebhookRequest });
    }
    for (const path of collectWecomAppRoutePaths(this.cfg.channels["wecom-app"])) {
      this.routes.push({ path, match: "prefix", handler: handleWecomAppWebhookRequest });
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
    const plugins = [
      { id: "qqbot", plugin: qqbotPlugin, config: this.cfg.channels.qqbot },
      { id: "dingtalk", plugin: dingtalkPlugin, config: this.cfg.channels.dingtalk },
      { id: "wecom", plugin: wecomPlugin, config: this.cfg.channels.wecom },
      { id: "wecom-app", plugin: wecomAppPlugin, config: this.cfg.channels["wecom-app"] },
      { id: "feishu-china", plugin: feishuPlugin, config: this.cfg.channels["feishu-china"] },
    ];
    const cfg = { channels: this.cfg.channels } as Record<string, unknown>;
    for (const item of plugins) {
      if (!item.config || item.config.enabled !== true) continue;
      const accountIds = item.plugin.config.listAccountIds(cfg);
      for (const accountId of accountIds) {
        const account = item.plugin.config.resolveAccount(cfg, accountId);
        const abort = new AbortController();
        this.aborts.set(`${item.id}:${accountId}`, abort);
        const task = Promise.resolve(
          item.plugin.gateway.startAccount({
            cfg,
            accountId,
            account,
            runtime: this.runtimeBridge.runtime,
            channelRuntime: this.runtimeBridge.channelRuntime,
            abortSignal: abort.signal,
            log: this.logger,
            getStatus: () => this.statuses.get(`${item.id}:${accountId}`) ?? { accountId },
            setStatus: (next: Record<string, unknown>) => {
              this.statuses.set(`${item.id}:${accountId}`, next);
            },
          })
        ).catch((err) => {
          this.logger.error(`${item.id}:${accountId} gateway failed: ${String(err)}`);
        });
        this.tasks.set(`${item.id}:${accountId}`, task);
      }
    }
  }
}
