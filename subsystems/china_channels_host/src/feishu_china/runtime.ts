/**
 * 飞书插件运行时管理
 *
 * 提供对 Moltbot 核心运行时的访问。
 */

/**
 * Moltbot 插件运行时接口
 */
export interface PluginRuntime {
  /** 日志函数 */
  log?: (msg: string) => void;
  /** 错误日志函数 */
  error?: (msg: string) => void;
  /** Moltbot 核心 API */
  channel?: {
    routing?: {
      resolveAgentRoute?: (params: {
        cfg: unknown;
        channel: string;
        peer: { kind: string; id: string };
      }) => { sessionKey: string; accountId: string; agentId?: string };
    };
    reply?: {
      dispatchReplyFromConfig?: (params: {
        ctx: unknown;
        cfg: unknown;
        dispatcher?: unknown;
        replyOptions?: unknown;
      }) => Promise<{ queuedFinal: boolean; counts: { final: number } }>;
      dispatchReplyWithBufferedBlockDispatcher?: (params: {
        ctx: unknown;
        cfg: unknown;
        dispatcherOptions: {
          deliver: (payload: unknown, info?: { kind?: string }) => Promise<void>;
          onError?: (err: unknown, info: { kind: string }) => void;
          onSkip?: (payload: unknown, info: { kind: string; reason: string }) => void;
          onReplyStart?: () => Promise<void> | void;
          humanDelay?: unknown;
        };
        replyOptions?: unknown;
      }) => Promise<unknown>;
      finalizeInboundContext?: (ctx: unknown) => unknown;
      createReplyDispatcher?: (params: unknown) => unknown;
      createReplyDispatcherWithTyping?: (params: unknown) => {
        dispatcher: unknown;
        replyOptions?: unknown;
        markDispatchIdle?: () => void;
      };
      resolveHumanDelayConfig?: (cfg: unknown, agentId?: string) => unknown;
    };
    text?: {
      resolveTextChunkLimit?: (params: {
        cfg: unknown;
        channel: string;
        defaultLimit?: number;
      }) => number;
      resolveChunkMode?: (cfg: unknown, channel: string) => unknown;
      chunkTextWithMode?: (text: string, limit: number, mode: unknown) => string[];
      chunkMarkdownText?: (text: string, limit: number) => string[];
    };
  };
  system?: {
    enqueueSystemEvent?: (message: string, options?: unknown) => void;
  };
  [key: string]: unknown;
}

/** 全局 runtime 实例 */
let runtime: PluginRuntime | null = null;

/**
 * 设置飞书插件运行时
 */
export function setFeishuRuntime(next: PluginRuntime): void {
  runtime = next;
}

/**
 * 获取飞书插件运行时
 */
export function getFeishuRuntime(): PluginRuntime {
  if (!runtime) {
    throw new Error("Feishu runtime not initialized. Make sure the plugin is properly registered with Moltbot.");
  }
  return runtime;
}

/**
 * 检查运行时是否已初始化
 */
export function isFeishuRuntimeInitialized(): boolean {
  return runtime !== null;
}

/**
 * 清除运行时（仅用于测试）
 */
export function clearFeishuRuntime(): void {
  runtime = null;
}
