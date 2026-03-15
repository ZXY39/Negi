import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  sendWecomAppMessage: vi.fn(),
  stripMarkdown: vi.fn((value: string) => value),
  downloadAndSendImage: vi.fn(),
  downloadAndSendVoice: vi.fn(),
  downloadAndSendFile: vi.fn(),
  downloadAndSendVideo: vi.fn(),
}));

vi.mock("./api.js", () => ({
  sendWecomAppMessage: mocks.sendWecomAppMessage,
  stripMarkdown: mocks.stripMarkdown,
  downloadAndSendImage: mocks.downloadAndSendImage,
  downloadAndSendVoice: mocks.downloadAndSendVoice,
  downloadAndSendFile: mocks.downloadAndSendFile,
  downloadAndSendVideo: mocks.downloadAndSendVideo,
}));

import { wecomAppPlugin } from "./channel.js";

function createCfg(voiceTranscode?: { enabled?: boolean }) {
  return {
    channels: {
      "wecom-app": {
        corpId: "corp-id",
        corpSecret: "corp-secret",
        agentId: 1000002,
        ...(voiceTranscode ? { voiceTranscode } : {}),
      },
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.downloadAndSendVoice.mockResolvedValue({ ok: true, msgid: "voice-msg" });
  mocks.downloadAndSendFile.mockResolvedValue({ ok: true, msgid: "file-msg" });
  mocks.downloadAndSendImage.mockResolvedValue({ ok: true, msgid: "image-msg" });
  mocks.downloadAndSendVideo.mockResolvedValue({ ok: true, msgid: "video-msg" });
  mocks.sendWecomAppMessage.mockResolvedValue({ ok: true, msgid: "text-msg" });
});

describe("wecom-app outbound media routing", () => {
  it("routes local wav to voice send and enables transcode by default", async () => {
    const result = await wecomAppPlugin.outbound.sendMedia({
      cfg: createCfg(),
      to: "user:alice",
      mediaUrl: "C:/tmp/demo.wav",
    });

    expect(result.ok).toBe(true);
    expect(mocks.downloadAndSendVoice).toHaveBeenCalledTimes(1);
    expect(mocks.downloadAndSendVoice).toHaveBeenCalledWith(
      expect.any(Object),
      { userId: "alice" },
      "C:/tmp/demo.wav",
      { contentType: undefined, transcode: true }
    );
    expect(mocks.downloadAndSendFile).not.toHaveBeenCalled();
  });

  it("routes remote audio urls with audio mime type to voice send", async () => {
    const result = await wecomAppPlugin.outbound.sendMedia({
      cfg: createCfg(),
      to: "user:alice",
      mediaUrl: "https://example.com/audio",
      mimeType: "audio/wav",
    });

    expect(result.ok).toBe(true);
    expect(mocks.downloadAndSendVoice).toHaveBeenCalledWith(
      expect.any(Object),
      { userId: "alice" },
      "https://example.com/audio",
      { contentType: "audio/wav", transcode: true }
    );
    expect(mocks.downloadAndSendFile).not.toHaveBeenCalled();
  });

  it("falls back to file send when transcode is disabled for unsupported audio", async () => {
    const result = await wecomAppPlugin.outbound.sendMedia({
      cfg: createCfg({ enabled: false }),
      to: "user:alice",
      mediaUrl: "C:/tmp/demo.mp3",
    });

    expect(result.ok).toBe(true);
    expect(mocks.downloadAndSendVoice).not.toHaveBeenCalled();
    expect(mocks.downloadAndSendFile).toHaveBeenCalledWith(
      expect.any(Object),
      { userId: "alice" },
      "C:/tmp/demo.mp3"
    );
  });

  it("still uses voice send for native amr when transcode is disabled", async () => {
    const result = await wecomAppPlugin.outbound.sendMedia({
      cfg: createCfg({ enabled: false }),
      to: "user:alice",
      mediaUrl: "C:/tmp/demo.amr",
    });

    expect(result.ok).toBe(true);
    expect(mocks.downloadAndSendVoice).toHaveBeenCalledWith(
      expect.any(Object),
      { userId: "alice" },
      "C:/tmp/demo.amr",
      { contentType: undefined, transcode: false }
    );
    expect(mocks.downloadAndSendFile).not.toHaveBeenCalled();
  });

  it("falls back to file send when transcoded voice send fails", async () => {
    mocks.downloadAndSendVoice.mockResolvedValueOnce({
      ok: false,
      errcode: 40007,
      errmsg: "voice send failed",
    });

    const result = await wecomAppPlugin.outbound.sendMedia({
      cfg: createCfg(),
      to: "user:alice",
      mediaUrl: "C:/tmp/demo.wav",
    });

    expect(mocks.downloadAndSendVoice).toHaveBeenCalledWith(
      expect.any(Object),
      { userId: "alice" },
      "C:/tmp/demo.wav",
      { contentType: undefined, transcode: true }
    );
    expect(mocks.downloadAndSendFile).toHaveBeenCalledWith(
      expect.any(Object),
      { userId: "alice" },
      "C:/tmp/demo.wav"
    );
    expect(result.ok).toBe(true);
    expect(result.messageId).toBe("file-msg");
  });

  it("does not fall back to file when native voice send fails", async () => {
    mocks.downloadAndSendVoice.mockResolvedValueOnce({
      ok: false,
      errcode: 40008,
      errmsg: "native voice send failed",
    });

    const result = await wecomAppPlugin.outbound.sendMedia({
      cfg: createCfg(),
      to: "user:alice",
      mediaUrl: "C:/tmp/demo.amr",
    });

    expect(mocks.downloadAndSendVoice).toHaveBeenCalledWith(
      expect.any(Object),
      { userId: "alice" },
      "C:/tmp/demo.amr",
      { contentType: undefined, transcode: true }
    );
    expect(mocks.downloadAndSendFile).not.toHaveBeenCalled();
    expect(result.ok).toBe(false);
    expect(result.error?.message).toBe("native voice send failed");
  });
});
