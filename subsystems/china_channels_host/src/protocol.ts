// @ts-nocheck
export type BridgeAuthFrame = {
  type: "auth";
  token: string;
  client: string;
};

export type InboundMessageFrame = {
  type: "inbound_message";
  event_id: string;
  channel: string;
  account_id: string;
  peer: { kind: string; id: string; display_name?: string };
  thread_id?: string;
  message: {
    id?: string;
    text?: string;
    attachments?: Array<Record<string, unknown>>;
  };
  metadata?: Record<string, unknown>;
};

export type DeliverMessageFrame = {
  type: "deliver_message";
  event_id: string;
  delivery_id: string;
  channel: string;
  account_id: string;
  target: { kind: string; id: string };
  reply_to?: string;
  payload: {
    text?: string;
    attachments?: Array<Record<string, unknown>>;
    mode?: string;
  };
  metadata?: Record<string, unknown>;
};

export type TurnCompleteFrame = { type: "turn_complete"; event_id: string };
export type TurnErrorFrame = { type: "turn_error"; event_id: string; error: string };
export type AuthOkFrame = { type: "auth_ok"; server: "china_channels_host"; version: string };

export type BridgeFrame =
  | BridgeAuthFrame
  | InboundMessageFrame
  | DeliverMessageFrame
  | TurnCompleteFrame
  | TurnErrorFrame
  | AuthOkFrame
  | Record<string, unknown>;

export function safeJsonParse(payload: string): BridgeFrame | null {
  try {
    return JSON.parse(payload) as BridgeFrame;
  } catch {
    return null;
  }
}
