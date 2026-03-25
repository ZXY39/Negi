import type { IncomingMessage, ServerResponse } from "http";

export type WecomKfDmPolicy = "open" | "pairing" | "allowlist" | "disabled";

export type WecomKfAccountConfig = {
  name?: string;
  enabled?: boolean;
  webhookPath?: string;
  token?: string;
  encodingAESKey?: string;
  corpId?: string;
  corpSecret?: string;
  openKfId?: string;
  apiBaseUrl?: string;
  welcomeText?: string;
  dmPolicy?: WecomKfDmPolicy;
  allowFrom?: string[];
};

export type WecomKfConfig = WecomKfAccountConfig & {
  accounts?: Record<string, WecomKfAccountConfig>;
  defaultAccount?: string;
};

export interface PluginConfig {
  session?: {
    store?: unknown;
  };
  channels?: Record<string, unknown> & {
    "wecom-kf"?: WecomKfConfig;
  };
  [key: string]: unknown;
}

export type ResolvedWecomKfAccount = {
  accountId: string;
  name?: string;
  enabled: boolean;
  configured: boolean;
  token?: string;
  encodingAESKey?: string;
  corpId?: string;
  corpSecret?: string;
  openKfId?: string;
  canSendActive: boolean;
  config: WecomKfAccountConfig;
};

export type AccessTokenCacheEntry = {
  token: string;
  expiresAt: number;
};

export type SyncMsgItemBase = {
  msgid: string;
  open_kfid: string;
  external_userid: string;
  send_time: number;
  origin: number;
  servicer_userid?: string;
};

export type SyncMsgText = SyncMsgItemBase & {
  msgtype: "text";
  text: { content: string; menu_id?: string };
};

export type SyncMsgEvent = SyncMsgItemBase & {
  msgtype: "event";
  event: {
    event_type: string;
    open_kfid?: string;
    external_userid?: string;
    scene?: string;
    scene_param?: string;
    welcome_code?: string;
    fail_msgid?: string;
    fail_type?: number;
    recall_msgid?: string;
  };
};

export type SyncMsgItem =
  | SyncMsgText
  | SyncMsgEvent
  | (SyncMsgItemBase & {
      msgtype: string;
      [key: string]: unknown;
    });

export type SyncMsgResponse = {
  errcode: number;
  errmsg: string;
  next_cursor: string;
  has_more: number;
  msg_list: SyncMsgItem[];
};

export type KfSendMsgParams = {
  touser?: string;
  open_kfid?: string;
  code?: string;
  msgid?: string;
  msgtype: string;
  [key: string]: unknown;
};

export type KfSendMsgResult = {
  errcode: number;
  errmsg: string;
  msgid?: string;
};

export type WecomKfAccountState = {
  configured?: boolean;
  running?: boolean;
  webhookPath?: string;
  hasCursor?: boolean;
  lastStartAt?: number;
  lastStopAt?: number;
  lastInboundAt?: number;
  lastOutboundAt?: number;
  lastSyncAt?: number;
  lastWelcomeAt?: number;
  lastError?: string;
};

export type WecomKfPersistedState = {
  version: 1;
  cursors: Record<string, string>;
  processedMsgIds: Record<string, number>;
  accounts: Record<string, WecomKfAccountState>;
};

export type WebhookTarget = {
  account: ResolvedWecomKfAccount;
  config: PluginConfig;
  runtime: {
    log: (message: string) => void;
    error: (message: string) => void;
  };
  path: string;
  statusSink?: (patch: Record<string, unknown>) => void;
};

export interface PluginRuntime {
  log?: (message: string) => void;
  error?: (message: string) => void;
  channel?: {
    routing?: {
      resolveAgentRoute?: (params: {
        cfg: unknown;
        channel: string;
        accountId?: string;
        peer: { kind: string; id: string };
      }) => {
        sessionKey: string;
        accountId: string;
        agentId?: string;
        mainSessionKey?: string;
      };
    };
    reply?: {
      dispatchReplyWithBufferedBlockDispatcher?: (params: {
        ctx: unknown;
        cfg: unknown;
        dispatcherOptions: {
          deliver: (payload: { text?: string }) => Promise<void>;
          onError?: (err: unknown, info: { kind: string }) => void;
        };
      }) => Promise<void>;
      finalizeInboundContext?: (ctx: unknown) => unknown;
      resolveEnvelopeFormatOptions?: (cfg: unknown) => unknown;
      formatAgentEnvelope?: (params: {
        channel: string;
        from: string;
        previousTimestamp?: number;
        envelope?: unknown;
        body: string;
      }) => string;
    };
    session?: {
      resolveStorePath?: (
        store: unknown,
        params: { agentId?: string }
      ) => string | undefined;
      readSessionUpdatedAt?: (params: {
        storePath?: string;
        sessionKey: string;
      }) => number | null;
      recordInboundSession?: (params: {
        storePath: string;
        sessionKey: string;
        ctx: unknown;
        updateLastRoute?: {
          sessionKey: string;
          channel: string;
          to: string;
          accountId?: string;
          threadId?: string | number;
        };
        onRecordError?: (err: unknown) => void;
      }) => Promise<void>;
    };
    text?: {
      resolveMarkdownTableMode?: (params: {
        cfg: unknown;
        channel: string;
        accountId?: string;
      }) => unknown;
      convertMarkdownTables?: (text: string, mode: unknown) => string;
    };
  };
  [key: string]: unknown;
}

type HttpRouteMatch = "exact" | "prefix";
type HttpRouteAuth = "gateway" | "plugin";

export type HttpRouteParams = {
  path: string;
  auth: HttpRouteAuth;
  match?: HttpRouteMatch;
  handler: (req: IncomingMessage, res: ServerResponse) => Promise<boolean> | boolean;
};

export interface MoltbotPluginApi {
  registerChannel: (opts: { plugin: unknown }) => void;
  registerCli?: (
    registrar: (ctx: { program: unknown; config?: PluginConfig }) => void | Promise<void>,
    opts?: { commands?: string[] }
  ) => void;
  registerHttpHandler?: (
    handler: (req: IncomingMessage, res: ServerResponse) => Promise<boolean> | boolean
  ) => void;
  registerHttpRoute?: (params: HttpRouteParams) => void;
  config?: PluginConfig;
  runtime?: unknown;
  [key: string]: unknown;
}
