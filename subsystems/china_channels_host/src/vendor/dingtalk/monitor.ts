/**
 * 兼容层：保持原 monitor.ts 导出不变，
 * 实际实现迁移到 bot-gateway.ts。
 */

export {
  monitorDingtalkProvider,
  stopDingtalkMonitorForAccount,
  stopAllDingtalkMonitors,
  stopDingtalkMonitor,
  isMonitorActiveForAccount,
  isMonitorActive,
  getActiveAccountIds,
  getCurrentAccountId,
  type MonitorDingtalkOpts,
} from "./bot-gateway.js";
