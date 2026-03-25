import os from "node:os";
import path from "node:path";
import { promises as fs } from "node:fs";

import { describe, expect, it, vi } from "vitest";

import { processMediaInMessage } from "./bot.js";
import type { WecomInboundMessage } from "./types.js";

const noopLogger = {
  debug: () => {},
  info: () => {},
  warn: () => {},
  error: () => {},
};

describe("wecom inbound media download", () => {
  it("uses the injected ws media downloader for inbound images", async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "wecom-sdk-download-image-"));
    const previousStateDir = process.env.OPENCLAW_STATE_DIR;
    process.env.OPENCLAW_STATE_DIR = tempDir;

    try {
      const downloadMedia = vi.fn(async () => ({
        buffer: Buffer.from("sdk-image", "utf8"),
        fileName: "reply.png",
      }));

      const result = await processMediaInMessage({
        msg: {
          msgtype: "image",
          image: {
            url: "https://example.test/media/reply.png",
            aeskey: "ws-aes-key",
          },
        } as WecomInboundMessage,
        downloadMedia,
        log: noopLogger,
      });

      expect(downloadMedia).toHaveBeenCalledWith(expect.objectContaining({
        mediaUrl: "https://example.test/media/reply.png",
        decryptionKey: "ws-aes-key",
      }));
      expect(result.imagePaths).toHaveLength(1);
      expect(result.text).toBe(`[image] ${result.imagePaths[0]}`);
      expect(await fs.readFile(result.imagePaths[0], "utf8")).toBe("sdk-image");
    } finally {
      if (previousStateDir === undefined) {
        delete process.env.OPENCLAW_STATE_DIR;
      } else {
        process.env.OPENCLAW_STATE_DIR = previousStateDir;
      }
      await fs.rm(tempDir, { recursive: true, force: true });
    }
  });

  it("uses the injected ws media downloader for inbound voice attachments", async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "wecom-sdk-download-voice-"));
    const previousStateDir = process.env.OPENCLAW_STATE_DIR;
    process.env.OPENCLAW_STATE_DIR = tempDir;

    try {
      const downloadMedia = vi.fn(async () => ({
        buffer: Buffer.from("sdk-voice", "utf8"),
        fileName: "voice.amr",
      }));

      const result = await processMediaInMessage({
        msg: {
          msgtype: "voice",
          voice: {
            url: "https://example.test/media/voice.amr",
            aeskey: "ws-aes-key",
          },
        } as WecomInboundMessage,
        downloadMedia,
        log: noopLogger,
      });

      expect(downloadMedia).toHaveBeenCalledWith(expect.objectContaining({
        mediaUrl: "https://example.test/media/voice.amr",
        decryptionKey: "ws-aes-key",
      }));
      expect(result.imagePaths).toEqual([]);
      expect(result.text.startsWith("[voice] ")).toBe(true);
      const savedPath = result.text.replace("[voice] ", "");
      expect(path.extname(savedPath)).toBe(".amr");
      expect(await fs.readFile(savedPath, "utf8")).toBe("sdk-voice");
    } finally {
      if (previousStateDir === undefined) {
        delete process.env.OPENCLAW_STATE_DIR;
      } else {
        process.env.OPENCLAW_STATE_DIR = previousStateDir;
      }
      await fs.rm(tempDir, { recursive: true, force: true });
    }
  });
});
