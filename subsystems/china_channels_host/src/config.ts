// @ts-nocheck
import fs from "node:fs";

import { listRegisteredChannels } from "./channel_registry.js";

export type ChinaHostConfig = {
  chinaBridge: {
    bindHost?: string;
    publicPort?: number;
    controlHost?: string;
    controlPort?: number;
    controlToken?: string;
    logLevel?: string;
    sendProgress?: boolean;
    sendToolHints?: boolean;
    channels?: Record<string, any>;
  };
  channels: Record<string, any>;
};

function resolveChannelPayload(channels: Record<string, any>, channelId: string, legacyKeys: string[]): any {
  const candidates = [channelId, channelId.replace(/-/g, "_"), channelId.replace(/-([a-z])/g, (_m, ch) => ch.toUpperCase()), ...legacyKeys];
  for (const key of candidates) {
    if (key in channels && channels[key] && typeof channels[key] === "object") {
      return channels[key];
    }
  }
  return {};
}

export function loadHostConfig(configPath: string): ChinaHostConfig {
  const raw = JSON.parse(fs.readFileSync(configPath, "utf-8")) as Record<string, any>;
  const chinaBridge = raw.chinaBridge && typeof raw.chinaBridge === "object" ? raw.chinaBridge : {};
  const channels = chinaBridge.channels && typeof chinaBridge.channels === "object" ? chinaBridge.channels : {};
  const channelMap: Record<string, any> = {};
  for (const item of listRegisteredChannels()) {
    const channelId = String(item.id || "").trim();
    if (!channelId) continue;
    channelMap[channelId] = resolveChannelPayload(channels, channelId, Array.isArray(item.legacy_keys) ? item.legacy_keys : []);
  }
  channelMap.sendProgress = chinaBridge.sendProgress ?? true;
  channelMap.sendToolHints = chinaBridge.sendToolHints ?? false;
  return {
    channels: channelMap,
    chinaBridge,
  };
}
