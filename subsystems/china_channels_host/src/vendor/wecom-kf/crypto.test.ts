import crypto from "node:crypto";

import { describe, expect, it } from "vitest";

import {
  computeWecomMsgSignature,
  decryptWecomEncrypted,
  verifyWecomSignature,
} from "./crypto.js";

const PKCS7_BLOCK_SIZE = 32;
const encodingAESKey = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG";
const receiveId = "ww-test-corp";

function decodeEncodingAESKey(raw: string): Buffer {
  return Buffer.from(raw.endsWith("=") ? raw : `${raw}=`, "base64");
}

function pkcs7Pad(buffer: Buffer, blockSize: number): Buffer {
  const mod = buffer.length % blockSize;
  const pad = mod === 0 ? blockSize : blockSize - mod;
  return Buffer.concat([buffer, Buffer.alloc(pad, pad)]);
}

function encryptWecomPlaintext(params: {
  encodingAESKey: string;
  receiveId: string;
  plaintext: string;
}): string {
  const aesKey = decodeEncodingAESKey(params.encodingAESKey);
  const iv = aesKey.subarray(0, 16);
  const random16 = Buffer.from("1234567890abcdef", "utf8");
  const plaintext = Buffer.from(params.plaintext, "utf8");
  const msgLength = Buffer.alloc(4);
  msgLength.writeUInt32BE(plaintext.length, 0);
  const payload = pkcs7Pad(
    Buffer.concat([random16, msgLength, plaintext, Buffer.from(params.receiveId, "utf8")]),
    PKCS7_BLOCK_SIZE
  );
  const cipher = crypto.createCipheriv("aes-256-cbc", aesKey, iv);
  cipher.setAutoPadding(false);
  return Buffer.concat([cipher.update(payload), cipher.final()]).toString("base64");
}

describe("wecom-kf crypto", () => {
  it("verifies callback signatures", () => {
    const signature = computeWecomMsgSignature({
      token: "callback-token",
      timestamp: "1710000000",
      nonce: "nonce-1",
      encrypt: "encrypted-body",
    });

    expect(
      verifyWecomSignature({
        token: "callback-token",
        timestamp: "1710000000",
        nonce: "nonce-1",
        encrypt: "encrypted-body",
        signature,
      })
    ).toBe(true);
    expect(
      verifyWecomSignature({
        token: "callback-token",
        timestamp: "1710000000",
        nonce: "nonce-1",
        encrypt: "encrypted-body",
        signature: "bad-signature",
      })
    ).toBe(false);
  });

  it("decrypts callback payloads and validates receiveId", () => {
    const plaintext = JSON.stringify({
      Token: "sync-token",
      OpenKfId: "wk-123",
    });
    const encrypted = encryptWecomPlaintext({
      encodingAESKey,
      receiveId,
      plaintext,
    });

    const decrypted = decryptWecomEncrypted({
      encodingAESKey,
      receiveId,
      encrypt: encrypted,
    });

    expect(decrypted).toBe(plaintext);
  });
});
