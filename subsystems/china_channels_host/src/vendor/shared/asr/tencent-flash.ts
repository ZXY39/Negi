import { createHmac } from "node:crypto";
import {
  ASRAuthError,
  ASREmptyResultError,
  ASRRequestError,
  ASRResponseParseError,
  ASRServiceError,
  ASRTimeoutError,
} from "./errors.js";

const ASR_FLASH_HOST = "asr.cloud.tencent.com";
const ASR_FLASH_PATH_PREFIX = "/asr/flash/v1";
const ASR_FLASH_URL_PREFIX = `https://${ASR_FLASH_HOST}${ASR_FLASH_PATH_PREFIX}`;
const ASR_PROVIDER = "tencent-flash";

export interface TencentFlashASRConfig {
  appId: string;
  secretId: string;
  secretKey: string;
  engineType?: string;
  voiceFormat?: string;
  timeoutMs?: number;
}

interface TencentFlashResponseSentence {
  text?: string;
}

interface TencentFlashResponseItem {
  text?: string;
  sentence_list?: TencentFlashResponseSentence[];
}

interface TencentFlashResponse {
  code?: number;
  message?: string;
  flash_result?: TencentFlashResponseItem[];
}

function encodeQueryValue(value: string): string {
  return encodeURIComponent(value)
    .replace(/%20/g, "+")
    .replace(/[!'()*]/g, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
}

function buildSignedQuery(params: Record<string, string>): string {
  return Object.entries(params)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeQueryValue(value)}`)
    .join("&");
}

function extractTranscript(payload: TencentFlashResponse): string {
  const items = Array.isArray(payload.flash_result) ? payload.flash_result : [];
  const lines: string[] = [];

  for (const item of items) {
    if (typeof item?.text === "string" && item.text.trim()) {
      lines.push(item.text.trim());
      continue;
    }
    const sentenceList = Array.isArray(item?.sentence_list) ? item.sentence_list : [];
    for (const sentence of sentenceList) {
      if (typeof sentence?.text === "string" && sentence.text.trim()) {
        lines.push(sentence.text.trim());
      }
    }
  }

  return lines.join("\n").trim();
}

export async function transcribeTencentFlash(params: {
  audio: Buffer;
  config: TencentFlashASRConfig;
}): Promise<string> {
  const { audio, config } = params;
  const timestamp = Math.floor(Date.now() / 1000).toString();
  const engineType = config.engineType ?? "16k_zh";
  const voiceFormat = config.voiceFormat ?? "silk";
  const query = buildSignedQuery({
    engine_type: engineType,
    secretid: config.secretId,
    timestamp,
    voice_format: voiceFormat,
  });

  const signText = `POST${ASR_FLASH_HOST}${ASR_FLASH_PATH_PREFIX}/${config.appId}?${query}`;
  const authorization = createHmac("sha1", config.secretKey).update(signText).digest("base64");
  const url = `${ASR_FLASH_URL_PREFIX}/${config.appId}?${query}`;
  const timeoutMs = config.timeoutMs ?? 30000;
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: authorization,
        "Content-Type": "application/octet-stream",
      },
      body: audio,
      signal: controller.signal,
    });

    const bodyText = await response.text();
    let payload: TencentFlashResponse;
    try {
      payload = JSON.parse(bodyText) as TencentFlashResponse;
    } catch {
      throw new ASRResponseParseError(ASR_PROVIDER, bodyText.slice(0, 300));
    }

    if (!response.ok) {
      const message = payload.message ?? `HTTP ${response.status}`;
      if (response.status === 401 || response.status === 403) {
        throw new ASRAuthError(
          ASR_PROVIDER,
          `Tencent Flash ASR authentication failed: ${message}`,
          response.status
        );
      }
      throw new ASRRequestError(
        ASR_PROVIDER,
        `Tencent Flash ASR request failed: ${message}`,
        response.status
      );
    }

    if (payload.code !== 0) {
      throw new ASRServiceError(
        ASR_PROVIDER,
        `Tencent Flash ASR failed: ${payload.message ?? "unknown error"} (code=${payload.code})`
        ,
        payload.code
      );
    }

    const transcript = extractTranscript(payload);
    if (!transcript) {
      throw new ASREmptyResultError(ASR_PROVIDER);
    }
    return transcript;
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new ASRTimeoutError(ASR_PROVIDER, timeoutMs);
    }
    if (
      error instanceof ASRResponseParseError ||
      error instanceof ASRAuthError ||
      error instanceof ASRRequestError ||
      error instanceof ASRServiceError ||
      error instanceof ASREmptyResultError ||
      error instanceof ASRTimeoutError
    ) {
      throw error;
    }
    throw new ASRRequestError(
      ASR_PROVIDER,
      `Tencent Flash ASR request failed: ${error instanceof Error ? error.message : String(error)}`
    );
  } finally {
    clearTimeout(timeoutId);
  }
}
