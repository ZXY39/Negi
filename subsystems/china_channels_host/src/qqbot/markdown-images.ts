import { Buffer } from "node:buffer";

export interface QQBotMarkdownImageSize {
  width: number;
  height: number;
}

export const DEFAULT_QQBOT_MARKDOWN_IMAGE_SIZE: QQBotMarkdownImageSize = {
  width: 512,
  height: 512,
};

const MARKDOWN_IMAGE_RE = /!\[([^\]]*)\]\(([^)\n]+)\)/g;
const BARE_HTTP_IMAGE_URL_RE =
  /(?<![(\["'<])(https?:\/\/[^\s)"'<>]+\.(?:png|jpe?g|gif|webp)(?:\?[^\s)"'<>]*)?)/gi;
const FENCED_CODE_BLOCK_RE = /(^|\n)(`{3,}|~{3,})[^\n]*\n[\s\S]*?\n\2(?=\n|$)/g;

type MarkdownSegment =
  | { kind: "text"; value: string }
  | { kind: "code"; value: string };

type NormalizeQQBotMarkdownImagesParams = {
  text: string;
  appendImageUrls?: string[];
  resolveImageSize?: (url: string) => Promise<QQBotMarkdownImageSize | null>;
  timeoutMs?: number;
};

function parsePngSize(buffer: Buffer): QQBotMarkdownImageSize | null {
  if (buffer.length < 24) return null;
  if (buffer[0] !== 0x89 || buffer[1] !== 0x50 || buffer[2] !== 0x4e || buffer[3] !== 0x47) {
    return null;
  }

  return {
    width: buffer.readUInt32BE(16),
    height: buffer.readUInt32BE(20),
  };
}

function parseJpegSize(buffer: Buffer): QQBotMarkdownImageSize | null {
  if (buffer.length < 4 || buffer[0] !== 0xff || buffer[1] !== 0xd8) {
    return null;
  }

  let offset = 2;
  while (offset < buffer.length - 9) {
    if (buffer[offset] !== 0xff) {
      offset += 1;
      continue;
    }

    const marker = buffer[offset + 1];
    if (marker === 0xc0 || marker === 0xc2) {
      return {
        height: buffer.readUInt16BE(offset + 5),
        width: buffer.readUInt16BE(offset + 7),
      };
    }

    if (offset + 3 >= buffer.length) {
      break;
    }

    const blockLength = buffer.readUInt16BE(offset + 2);
    offset += 2 + blockLength;
  }

  return null;
}

function parseGifSize(buffer: Buffer): QQBotMarkdownImageSize | null {
  if (buffer.length < 10) return null;
  const signature = buffer.toString("ascii", 0, 6);
  if (signature !== "GIF87a" && signature !== "GIF89a") {
    return null;
  }

  return {
    width: buffer.readUInt16LE(6),
    height: buffer.readUInt16LE(8),
  };
}

function parseWebpSize(buffer: Buffer): QQBotMarkdownImageSize | null {
  if (buffer.length < 30) return null;

  if (buffer.toString("ascii", 0, 4) !== "RIFF" || buffer.toString("ascii", 8, 12) !== "WEBP") {
    return null;
  }

  const chunkType = buffer.toString("ascii", 12, 16);
  if (chunkType === "VP8 " && buffer[23] === 0x9d && buffer[24] === 0x01 && buffer[25] === 0x2a) {
    return {
      width: buffer.readUInt16LE(26) & 0x3fff,
      height: buffer.readUInt16LE(28) & 0x3fff,
    };
  }

  if (chunkType === "VP8L" && buffer[20] === 0x2f) {
    const bits = buffer.readUInt32LE(21);
    return {
      width: (bits & 0x3fff) + 1,
      height: ((bits >> 14) & 0x3fff) + 1,
    };
  }

  if (chunkType === "VP8X") {
    return {
      width: (buffer[24] | (buffer[25] << 8) | (buffer[26] << 16)) + 1,
      height: (buffer[27] | (buffer[28] << 8) | (buffer[29] << 16)) + 1,
    };
  }

  return null;
}

function parseImageSize(buffer: Buffer): QQBotMarkdownImageSize | null {
  return parsePngSize(buffer) ?? parseJpegSize(buffer) ?? parseGifSize(buffer) ?? parseWebpSize(buffer);
}

function normalizeImageSize(
  size: QQBotMarkdownImageSize | null | undefined
): QQBotMarkdownImageSize | null {
  if (!size) return null;
  if (!Number.isFinite(size.width) || !Number.isFinite(size.height)) return null;
  if (size.width <= 0 || size.height <= 0) return null;
  return {
    width: Math.round(size.width),
    height: Math.round(size.height),
  };
}

function splitMarkdownImageDestination(rawDestination: string): string {
  let next = rawDestination.trim();
  const whitespaceIndex = next.search(/\s/);
  if (whitespaceIndex >= 0) {
    next = next.slice(0, whitespaceIndex);
  }
  if (next.startsWith("<") && next.endsWith(">")) {
    next = next.slice(1, -1).trim();
  }
  return next;
}

function splitFencedCodeBlocks(text: string): MarkdownSegment[] {
  const segments: MarkdownSegment[] = [];
  const re = new RegExp(FENCED_CODE_BLOCK_RE.source, FENCED_CODE_BLOCK_RE.flags);
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = re.exec(text)) !== null) {
    const leading = match[1] ?? "";
    const codeStart = match.index + leading.length;
    if (codeStart > lastIndex) {
      segments.push({ kind: "text", value: text.slice(lastIndex, codeStart) });
    }
    segments.push({ kind: "code", value: text.slice(codeStart, re.lastIndex) });
    lastIndex = re.lastIndex;
  }

  if (lastIndex < text.length) {
    segments.push({ kind: "text", value: text.slice(lastIndex) });
  }

  return segments;
}

async function replaceAsync(
  input: string,
  pattern: RegExp,
  replacer: (match: RegExpExecArray) => Promise<string>
): Promise<string> {
  const re = new RegExp(pattern.source, pattern.flags);
  let result = "";
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = re.exec(input)) !== null) {
    result += input.slice(lastIndex, match.index);
    result += await replacer(match);
    lastIndex = match.index + match[0].length;
    if (match[0].length === 0) {
      re.lastIndex += 1;
    }
  }

  result += input.slice(lastIndex);
  return result;
}

async function getResolvedImageSize(params: {
  url: string;
  cache: Map<string, Promise<QQBotMarkdownImageSize>>;
  resolveImageSize: (url: string) => Promise<QQBotMarkdownImageSize | null>;
}): Promise<QQBotMarkdownImageSize> {
  const { url, cache, resolveImageSize } = params;
  const existing = cache.get(url);
  if (existing) {
    return existing;
  }

  const pending = resolveImageSize(url)
    .then((size) => normalizeImageSize(size) ?? DEFAULT_QQBOT_MARKDOWN_IMAGE_SIZE)
    .catch(() => DEFAULT_QQBOT_MARKDOWN_IMAGE_SIZE);
  cache.set(url, pending);
  return pending;
}

async function normalizeTextSegment(params: {
  text: string;
  seenImageUrls: Set<string>;
  imageSizeCache: Map<string, Promise<QQBotMarkdownImageSize>>;
  resolveImageSize: (url: string) => Promise<QQBotMarkdownImageSize | null>;
}): Promise<string> {
  const { text, seenImageUrls, imageSizeCache, resolveImageSize } = params;

  const withMarkdownImages = await replaceAsync(text, MARKDOWN_IMAGE_RE, async (match) => {
    const fullMatch = match[0];
    const destination = splitMarkdownImageDestination(match[2] ?? "");
    if (!isQQBotHttpImageUrl(destination)) {
      return fullMatch;
    }

    seenImageUrls.add(destination);
    if (hasQQBotMarkdownImageSize(fullMatch)) {
      return fullMatch;
    }

    const size = await getResolvedImageSize({
      url: destination,
      cache: imageSizeCache,
      resolveImageSize,
    });
    return formatQQBotMarkdownImage(destination, size);
  });

  return replaceAsync(withMarkdownImages, BARE_HTTP_IMAGE_URL_RE, async (match) => {
    const url = match[1] ?? match[0];
    if (!isQQBotHttpImageUrl(url)) {
      return match[0];
    }

    seenImageUrls.add(url);
    const size = await getResolvedImageSize({
      url,
      cache: imageSizeCache,
      resolveImageSize,
    });
    return formatQQBotMarkdownImage(url, size);
  });
}

export function isQQBotHttpImageUrl(url: string): boolean {
  const trimmed = url.trim();
  if (!/^https?:\/\//i.test(trimmed)) {
    return false;
  }

  try {
    const parsed = new URL(trimmed);
    return /\.(?:png|jpe?g|gif|webp)$/i.test(parsed.pathname);
  } catch {
    return /\.(?:png|jpe?g|gif|webp)(?:\?[^\s)"'<>]*)?$/i.test(trimmed);
  }
}

export async function getQQBotHttpImageSize(
  url: string,
  timeoutMs = 5000
): Promise<QQBotMarkdownImageSize | null> {
  if (!isQQBotHttpImageUrl(url)) {
    return null;
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, {
      signal: controller.signal,
      headers: {
        Range: "bytes=0-65535",
        "User-Agent": "OpenClaw-QQBot-ImageSize/1.0",
      },
    });
    if (!(response.ok || response.status === 206)) {
      return null;
    }

    const buffer = Buffer.from(await response.arrayBuffer());
    return normalizeImageSize(parseImageSize(buffer));
  } catch {
    return null;
  } finally {
    clearTimeout(timeoutId);
  }
}

export function formatQQBotMarkdownImage(
  url: string,
  size?: QQBotMarkdownImageSize | null
): string {
  const resolved = normalizeImageSize(size) ?? DEFAULT_QQBOT_MARKDOWN_IMAGE_SIZE;
  return `![#${resolved.width}px #${resolved.height}px](${url})`;
}

export function hasQQBotMarkdownImageSize(markdownImage: string): boolean {
  return /!\[#\d+px\s+#\d+px\]\([^)]+\)/.test(markdownImage);
}

export async function normalizeQQBotMarkdownImages(
  params: NormalizeQQBotMarkdownImagesParams
): Promise<string> {
  const text = params.text ?? "";
  const appendImageUrls = params.appendImageUrls ?? [];
  const resolveImageSize =
    params.resolveImageSize ?? ((url: string) => getQQBotHttpImageSize(url, params.timeoutMs));
  const imageSizeCache = new Map<string, Promise<QQBotMarkdownImageSize>>();
  const seenImageUrls = new Set<string>();

  const segments = splitFencedCodeBlocks(text);
  const normalizedSegments: string[] = [];
  for (const segment of segments) {
    if (segment.kind === "code") {
      normalizedSegments.push(segment.value);
      continue;
    }

    normalizedSegments.push(
      await normalizeTextSegment({
        text: segment.value,
        seenImageUrls,
        imageSizeCache,
        resolveImageSize,
      })
    );
  }

  const appendedImages: string[] = [];
  for (const rawUrl of appendImageUrls) {
    const url = rawUrl.trim();
    if (!isQQBotHttpImageUrl(url) || seenImageUrls.has(url)) {
      continue;
    }

    seenImageUrls.add(url);
    const size = await getResolvedImageSize({
      url,
      cache: imageSizeCache,
      resolveImageSize,
    });
    appendedImages.push(formatQQBotMarkdownImage(url, size));
  }

  const body = normalizedSegments.join("").trim();
  if (appendedImages.length === 0) {
    return body;
  }
  if (!body) {
    return appendedImages.join("\n");
  }

  return `${body}\n\n${appendedImages.join("\n")}`;
}
