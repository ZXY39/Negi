// 飞书类型定义

import type { FeishuConfig } from "./config.js";

export type { FeishuConfig };

export interface FeishuMention {
  key?: string;
  id?: {
    open_id?: string;
    user_id?: string;
    union_id?: string;
  };
  name?: string;
}

export interface FeishuMessageEvent {
  sender?: {
    sender_id?: {
      open_id?: string;
      user_id?: string;
      union_id?: string;
    };
  };
  message?: {
    message_id?: string;
    chat_id?: string;
    chat_type?: "p2p" | "group";
    message_type?: string;
    content?: string;
    create_time?: string;
    mentions?: FeishuMention[];
  };
}

/**
 * 解析后的消息上下文
 * 用于内部处理的标准化消息格式
 */
export interface FeishuMessageContext {
  /** 会话 ID */
  chatId: string;
  /** 消息 ID */
  messageId: string;
  /** 发送者 ID（open_id / user_id） */
  senderId: string;
  /** 聊天类型: direct = 单聊, group = 群聊 */
  chatType: "direct" | "group";
  /** 消息内容 */
  content: string;
  /** 内容类型 */
  contentType: string;
  /** 是否 @提及了机器人 */
  mentionedBot: boolean;
}

/**
 * 发送消息结果
 */
export interface FeishuSendResult {
  /** 消息 ID */
  messageId: string;
  /** 会话 ID */
  chatId: string;
}

/**
 * 解析后的飞书账户配置
 * 用于 ChannelPlugin config 适配器
 */
export interface ResolvedFeishuAccount {
  /** 账户 ID */
  accountId: string;
  /** 是否启用 */
  enabled: boolean;
  /** 是否已配置（有凭证） */
  configured: boolean;
  /** 应用 ID */
  appId?: string;
}
