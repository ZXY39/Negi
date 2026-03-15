import fs from "node:fs";

export type ChinaHostConfig = {
  channels: Record<string, any>;
  chinaBridge: {
    bindHost?: string;
    publicPort?: number;
    controlHost?: string;
    controlPort?: number;
    controlToken?: string;
    logLevel?: string;
  };
};

export function loadHostConfig(configPath: string): ChinaHostConfig {
  const raw = JSON.parse(fs.readFileSync(configPath, "utf-8")) as Record<string, any>;
  const channels = raw.channels && typeof raw.channels === "object" ? raw.channels : {};
  const chinaBridge = raw.chinaBridge && typeof raw.chinaBridge === "object" ? raw.chinaBridge : {};
  return {
    channels: {
      qqbot: channels.qqbot ?? {},
      dingtalk: channels.dingtalk ?? {},
      wecom: channels.wecom ?? {},
      "wecom-app": channels.wecomApp ?? channels.wecom_app ?? {},
      "feishu-china": channels.feishuChina ?? channels.feishu_china ?? {},
      sendProgress: channels.sendProgress ?? true,
      sendToolHints: channels.sendToolHints ?? false,
    },
    chinaBridge,
  };
}
