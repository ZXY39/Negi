import crypto from "node:crypto";

const WECOM_PKCS7_BLOCK_SIZE = 32;

function decodeEncodingAESKey(encodingAESKey: string): Buffer {
  const trimmed = encodingAESKey.trim();
  if (!trimmed) throw new Error("encodingAESKey missing");
  const withPadding = trimmed.endsWith("=") ? trimmed : `${trimmed}=`;
  const decoded = Buffer.from(withPadding, "base64");
  if (decoded.length !== 32) {
    throw new Error(
      `invalid encodingAESKey (expected 32 bytes after base64 decode, got ${decoded.length})`
    );
  }
  return decoded;
}

function pkcs7Unpad(buffer: Buffer): Buffer {
  if (buffer.length === 0) throw new Error("invalid pkcs7 payload");
  const pad = buffer[buffer.length - 1];
  if (pad < 1 || pad > WECOM_PKCS7_BLOCK_SIZE || pad > buffer.length) {
    throw new Error("invalid pkcs7 padding");
  }
  for (let index = 1; index <= pad; index += 1) {
    if (buffer[buffer.length - index] !== pad) {
      throw new Error("invalid pkcs7 padding");
    }
  }
  return buffer.subarray(0, buffer.length - pad);
}

export function computeWecomMsgSignature(params: {
  token: string;
  timestamp: string;
  nonce: string;
  encrypt: string;
}): string {
  return crypto
    .createHash("sha1")
    .update(
      [params.token, params.timestamp, params.nonce, params.encrypt]
        .map((value) => String(value ?? ""))
        .sort()
        .join("")
    )
    .digest("hex");
}

export function verifyWecomSignature(params: {
  token: string;
  timestamp: string;
  nonce: string;
  encrypt: string;
  signature: string;
}): boolean {
  return (
    computeWecomMsgSignature({
      token: params.token,
      timestamp: params.timestamp,
      nonce: params.nonce,
      encrypt: params.encrypt,
    }) === params.signature
  );
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
  const decrypted = pkcs7Unpad(decryptedPadded);
  if (decrypted.length < 20) {
    throw new Error(`invalid decrypted payload length ${decrypted.length}`);
  }
  const msgLength = decrypted.readUInt32BE(16);
  const msgStart = 20;
  const msgEnd = msgStart + msgLength;
  if (msgEnd > decrypted.length) {
    throw new Error("invalid decrypted msg length");
  }
  const msg = decrypted.subarray(msgStart, msgEnd).toString("utf8");
  const receiveId = params.receiveId?.trim();
  if (receiveId) {
    const trailing = decrypted.subarray(msgEnd).toString("utf8");
    if (trailing !== receiveId) {
      throw new Error(`receiveId mismatch (expected "${receiveId}", got "${trailing}")`);
    }
  }
  return msg;
}
