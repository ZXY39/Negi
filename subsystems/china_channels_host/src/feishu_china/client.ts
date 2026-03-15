/**
 * 飞书消息客户端
 * 负责发送消息
 */

import * as lark from "@larksuiteoapi/node-sdk";
import type { FeishuConfig } from "./config.js";

// 客户端缓存
const clientCache = new Map<string, lark.Client>();

/**
 * 获取或创建飞书客户端
 */
export function createFeishuClientFromConfig(config: FeishuConfig): lark.Client {
  const cacheKey = config.appId ?? "";

  let client = clientCache.get(cacheKey);
  if (!client) {
    if (!config.appId || !config.appSecret) {
      throw new Error("Feishu appId/appSecret missing");
    }
    client = new lark.Client({
      appId: config.appId,
      appSecret: config.appSecret,
    });
    clientCache.set(cacheKey, client);
  }

  return client;
}
