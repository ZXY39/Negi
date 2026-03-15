import crypto from "node:crypto";

import { describe, expect, it } from "vitest";

import { decryptWecomMedia } from "./crypto.js";

const PKCS7_BLOCK_SIZE = 32;

function pkcs7Pad(buffer: Buffer, blockSize: number): Buffer {
  const mod = buffer.length % blockSize;
  const pad = mod === 0 ? blockSize : blockSize - mod;
  return Buffer.concat([buffer, Buffer.alloc(pad, pad)]);
}

function encryptMedia(plaintext: Buffer, key: string): Buffer {
  const aesKey = Buffer.from(key, "utf8");
  const iv = aesKey.subarray(0, 16);
  const cipher = crypto.createCipheriv("aes-256-cbc", aesKey, iv);
  cipher.setAutoPadding(false);
  const padded = pkcs7Pad(plaintext, PKCS7_BLOCK_SIZE);
  return Buffer.concat([cipher.update(padded), cipher.final()]);
}

describe("wecom media crypto", () => {
  it("decrypts ws media using the per-message aeskey", () => {
    const key = "12345678901234567890123456789012";
    const encrypted = encryptMedia(Buffer.from("hello ws", "utf8"), key);

    const decrypted = decryptWecomMedia({
      encryptedBuffer: encrypted,
      decryptionKey: key,
    });

    expect(decrypted.toString("utf8")).toBe("hello ws");
  });

  it("decrypts webhook media using a base64-encoded encodingAESKey", () => {
    const key = "abcdefghijklmnopqrstuvwxyz123456";
    const encrypted = encryptMedia(Buffer.from("hello webhook", "utf8"), key);
    const encodingAESKey = Buffer.from(key, "utf8").toString("base64").replace(/=+$/u, "");

    const decrypted = decryptWecomMedia({
      encryptedBuffer: encrypted,
      decryptionKey: encodingAESKey,
    });

    expect(decrypted.toString("utf8")).toBe("hello webhook");
  });
});
