import { describe, expect, it, vi } from "vitest";

import { G3kuRuntimeBridge } from "./runtime_bridge.js";

describe("G3kuRuntimeBridge late final delivery", () => {
  it("routes late final replies through the last known session deliver callback", async () => {
    const bridge = new G3kuRuntimeBridge({
      host: "127.0.0.1",
      port: 18989,
      token: "token",
      version: "test",
      channelsConfig: {},
    }) as any;
    const send = vi.fn();
    bridge.send = send;

    const deliver = vi.fn().mockResolvedValue(undefined);
    const pending = bridge.dispatchTurn({
      ctx: {
        OriginatingChannel: "qqbot",
        AccountId: "default",
        OriginatingTo: "user:user-42",
        Body: "hello",
        SessionKey: "china:qqbot:default:dm:user-42",
      },
      dispatcherOptions: {
        deliver,
      },
    });

    expect(send).toHaveBeenCalledTimes(1);
    const eventId = send.mock.calls[0]?.[0]?.event_id;
    expect(eventId).toBeTruthy();

    await bridge.handleFrame({
      type: "turn_complete",
      event_id: eventId,
    });

    await expect(pending).resolves.toEqual({
      queuedFinal: false,
      counts: { final: 0 },
    });

    const lateText = "已通过异步任务 task:demo 完成交付";
    await bridge.handleFrame({
      type: "deliver_message",
      event_id: "evt-late",
      delivery_id: "delivery-late",
      channel: "qqbot",
      account_id: "default",
      target: { kind: "user", id: "user-42" },
      payload: { text: lateText, mode: "final" },
      metadata: { session_key: "china:qqbot:default:dm" },
    });

    expect(deliver).toHaveBeenCalledTimes(1);
    expect(deliver).toHaveBeenCalledWith(
      {
        text: lateText,
        mediaUrls: undefined,
        mediaUrl: undefined,
      },
      { kind: "final" },
    );
  });
});
