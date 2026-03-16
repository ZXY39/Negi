// @ts-nocheck
import fs from "node:fs";

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

export function loadHostConfig(configPath: string): ChinaHostConfig {
  const raw = JSON.parse(fs.readFileSync(configPath, "utf-8")) as Record<string, any>;
  const chinaBridge = raw.chinaBridge && typeof raw.chinaBridge === "object" ? raw.chinaBridge : {};
  const channels = chinaBridge.channels && typeof chinaBridge.channels === "object" ? chinaBridge.channels : {};
  return {
    channels: {
      qqbot: channels.qqbot ?? {},
      dingtalk: channels.dingtalk ?? {},
      wecom: channels.wecom ?? {},
      "wecom-app": channels.wecomApp ?? channels.wecom_app ?? {},
      "feishu-china": channels.feishuChina ?? channels.feishu_china ?? {},
      sendProgress: chinaBridge.sendProgress ?? true,
      sendToolHints: chinaBridge.sendToolHints ?? false,
    },
    chinaBridge,
  };
}
