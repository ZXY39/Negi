import crypto from "node:crypto";

function decodeEncodingAESKey(encodingAESKey: string): Buffer {
  const trimmed = encodingAESKey.trim();
  if (!trimmed) throw new Error("encodingAESKey missing");
  const withPadding = trimmed.endsWith("=") ? trimmed : `${trimmed}=`;
  const key = Buffer.from(withPadding, "base64");
  if (key.length !== 32) {
    throw new Error(`invalid encodingAESKey (expected 32 bytes after base64 decode, got ${key.length})`);
  }
  return key;
}

function decodeWecomMediaAESKey(rawKey: string): Buffer {
  const trimmed = rawKey.trim();
  if (!trimmed) throw new Error("media AES key missing");

  const utf8Key = Buffer.from(trimmed, "utf8");
  if (utf8Key.length === 32) {
    return utf8Key;
  }

  const withPadding = trimmed.endsWith("=") ? trimmed : `${trimmed}=`;
  const base64Key = Buffer.from(withPadding, "base64");
  if (base64Key.length === 32) {
    return base64Key;
  }

  throw new Error(`invalid media AES key (expected 32 bytes, got utf8=${utf8Key.length}, base64=${base64Key.length})`);
}

const WECOM_PKCS7_BLOCK_SIZE = 32;

function pkcs7Pad(buf: Buffer, blockSize: number): Buffer {
  const mod = buf.length % blockSize;
  const pad = mod === 0 ? blockSize : blockSize - mod;
  return Buffer.concat([buf, Buffer.alloc(pad, pad)]);
}

function pkcs7Unpad(buf: Buffer, blockSize: number): Buffer {
  if (buf.length === 0) throw new Error("invalid pkcs7 payload");
  const pad = buf[buf.length - 1];
  if (!pad || pad < 1 || pad > blockSize || pad > buf.length) {
    throw new Error("invalid pkcs7 padding");
  }
  for (let i = 1; i <= pad; i += 1) {
    if (buf[buf.length - i] !== pad) {
      throw new Error("invalid pkcs7 padding");
    }
  }
  return buf.subarray(0, buf.length - pad);
}

function sha1Hex(input: string): string {
  return crypto.createHash("sha1").update(input).digest("hex");
}

export function computeWecomMsgSignature(params: {
  token: string;
  timestamp: string;
  nonce: string;
  encrypt: string;
}): string {
  const parts = [params.token, params.timestamp, params.nonce, params.encrypt]
    .map((value) => String(value ?? ""))
    .sort();
  return sha1Hex(parts.join(""));
}

export function verifyWecomSignature(params: {
  token: string;
  timestamp: string;
  nonce: string;
  encrypt: string;
  signature: string;
}): boolean {
  const expected = computeWecomMsgSignature({
    token: params.token,
    timestamp: params.timestamp,
    nonce: params.nonce,
    encrypt: params.encrypt,
  });
  return expected === params.signature;
}

export function decryptWecomEncrypted(params: {
  encodingAESKey: string;
  receiveId?: string;
  encrypt: string;
}): string {
  const aesKey = decodeEncodingAESKey(params.encodingAESKey);
  const iv = aesKey.subarray(0, 16);
  const decipher = crypto.createDecipheriv("aes-256-cbc", aesKey, iv);
  decipher.setAutoPadding(false);
  const decryptedPadded = Buffer.concat([
    decipher.update(Buffer.from(params.encrypt, "base64")),
    decipher.final(),
  ]);
  const decrypted = pkcs7Unpad(decryptedPadded, WECOM_PKCS7_BLOCK_SIZE);

  if (decrypted.length < 20) {
    throw new Error(`invalid decrypted payload (expected at least 20 bytes, got ${decrypted.length})`);
  }

  const msgLen = decrypted.readUInt32BE(16);
  const msgStart = 20;
  const msgEnd = msgStart + msgLen;
  if (msgEnd > decrypted.length) {
    throw new Error(`invalid decrypted msg length (msgEnd=${msgEnd}, payloadLength=${decrypted.length})`);
  }

  const msg = decrypted.subarray(msgStart, msgEnd).toString("utf8");

  const receiveId = params.receiveId ?? "";
  if (receiveId) {
    const trailing = decrypted.subarray(msgEnd).toString("utf8");
    if (trailing !== receiveId) {
      throw new Error(`receiveId mismatch (expected "${receiveId}", got "${trailing}")`);
    }
  }

  return msg;
}

export function encryptWecomPlaintext(params: {
  encodingAESKey: string;
  receiveId?: string;
  plaintext: string;
}): string {
  const aesKey = decodeEncodingAESKey(params.encodingAESKey);
  const iv = aesKey.subarray(0, 16);
  const random16 = crypto.randomBytes(16);
  const msg = Buffer.from(params.plaintext ?? "", "utf8");
  const msgLen = Buffer.alloc(4);
  msgLen.writeUInt32BE(msg.length, 0);
  const receiveId = Buffer.from(params.receiveId ?? "", "utf8");

  const raw = Buffer.concat([random16, msgLen, msg, receiveId]);
  const padded = pkcs7Pad(raw, WECOM_PKCS7_BLOCK_SIZE);
  const cipher = crypto.createCipheriv("aes-256-cbc", aesKey, iv);
  cipher.setAutoPadding(false);
  const encrypted = Buffer.concat([cipher.update(padded), cipher.final()]);
  return encrypted.toString("base64");
}

/**
 * 解密企业微信媒体文件
 *
 * 企业微信接收到的媒体文件（图片/文件/语音）使用 AES-256-CBC 加密
 * IV 为 encodingAESKey 的前 16 字节，使用 PKCS#7 填充
 *
 * @param params 解密参数
 * @returns 解密后的 Buffer
 * @throws Error 如果解密失败
 *
 * @example
 * ```typescript
 * const decrypted = decryptWecomMedia({
 *   encryptedBuffer: encryptedData,
 *   encodingAESKey: "your_encoding_aes_key",
 * });
 * ```
 *
 * 参考: https://developer.work.weixin.qq.com/document/path/100719
 */
export function decryptWecomMedia(params: {
  /** 加密的媒体数据 Buffer */
  encryptedBuffer: Buffer;
  /** webhook 模式使用 encodingAESKey，长连接模式使用每条消息返回的 aeskey */
  decryptionKey: string;
}): Buffer {
  const { encryptedBuffer, decryptionKey } = params;

  if (!encryptedBuffer || encryptedBuffer.length === 0) {
    throw new Error("encryptedBuffer cannot be empty");
  }

  const aesKey = decodeWecomMediaAESKey(decryptionKey);

  // IV 为 AES Key 的前 16 字节
  const iv = aesKey.subarray(0, 16);

  // 创建解密器
  const decipher = crypto.createDecipheriv("aes-256-cbc", aesKey, iv);
  decipher.setAutoPadding(false);

  try {
    // 解密数据
    const decryptedPadded = Buffer.concat([
      decipher.update(encryptedBuffer),
      decipher.final(),
    ]);

    // 移除 PKCS#7 填充
    const decrypted = pkcs7Unpad(decryptedPadded, WECOM_PKCS7_BLOCK_SIZE);

    return decrypted;
  } catch (err) {
    throw new Error(
      `Failed to decrypt media: ${err instanceof Error ? err.message : String(err)}`
    );
  }
}
