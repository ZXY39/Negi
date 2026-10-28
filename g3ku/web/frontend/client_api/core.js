"use strict";

const API_BASE_URL = "";
const FALLBACK_SESSION_ID = "web:shared";
const MODEL_SAVE_TIMEOUT_MS = 30000;

// 唯一发请求的地方：URL 组装、超时、Abort、错误码归一。域模块只声明端点与响应归一。
class ApiCore {
    static _activeSessionId = "";

    static _requestControllers = new Map();

    static _requestTokens = new Map();

    static _bootstrapUnlockRequestKey = "bootstrap:unlock";

    static _normalizeErrorCode(value) {
        if (typeof value !== "string") return "";
        const normalized = value.trim();
        if (!normalized) return "";
        return /^[a-z0-9_]+$/i.test(normalized) ? normalized.toLowerCase() : "";
    }

    static getErrorCode(value) {
        if (!value) return "";
        if (typeof value === "string") return this._normalizeErrorCode(value);
        if (typeof value !== "object") return "";
        return this._normalizeErrorCode(value.code)
            || this.getErrorCode(value.data)
            || this.getErrorCode(value.detail)
            || this.getErrorCode(value.payload)
            || this.getErrorCode(value.message)
            || "";
    }

    static _friendlyErrorMessageForCode(code) {
        switch (String(code || "").trim().toLowerCase()) {
            case "no_model_configured":
                return "当前项目还没有配置可用模型。请先进入“模型配置”页面，新增并保存至少一个模型，并把它分配给主Agent（Leader）角色。";
            case "project_locked":
                return "项目当前已锁定，请先完成解锁后再继续。";
            case "bundle_password_invalid":
                return "配置包口令不正确。";
            case "bundle_current_password_required":
                return "导入前先输入当前项目解锁口令，确认是这台机器的操作者。";
            case "bundle_current_password_invalid":
                return "当前项目解锁口令不正确，导入未执行，本机内容没有改动。";
            case "bundle_project_password_unavailable":
                return "这个项目没有设置解锁密码（只用环境变量主密钥解锁），请改用自定义导出口令。";
            case "bundle_password_required":
                return "请输入口令。";
            case "bundle_scope_empty":
                return "请先勾选要导出或导入的范围（配置 / 记忆 / 人设 / 表情包）。";
            case "bundle_scope_not_in_package":
                return "这个包里没有你勾选的那一面，请改勾选或换一个包。";
            case "bundle_memory_store_missing":
                return "这台机器还没有记忆库，导不出记忆。";
            case "bundle_persona_empty":
                return "这台机器还没有人设描述或人设图，导不出人设。";
            case "bundle_stickers_empty":
                return "这台机器还没有表情包，导不出表情包。";
            case "bundle_file_invalid":
                return "这个文件不是可用的配置包，或包内密钥已损坏。";
            case "bundle_version_unsupported":
                return "配置包版本比当前程序更新，请先升级后再导入。";
            case "bundle_path_rejected":
                return "配置包内含越界路径，已拒绝写入。";
            case "config_bundle_failed":
                return "配置包处理失败。";
            case "bundle_staging_failed":
                return "服务器暂存上传文件失败（磁盘或权限），配置未改动。";
            case "task_service_unavailable":
                return "任务运行服务暂未就绪，请稍后再试。";
            case "main_task_service_unavailable":
                return "主任务服务暂未就绪，请稍后再试。";
            case "task_worker_starting":
                return "Task worker is starting. Controls will be available shortly.";
            case "task_worker_stale":
                return "Task worker status is temporarily stale. Wait for reconnection and try again.";
            case "task_worker_offline":
                return "Task worker is offline. Controls are unavailable right now.";
            case "memory_manager_unavailable":
                return "记忆服务暂不可用，请稍后刷新。";
            case "memory_queue_read_failed":
                return "记忆队列暂时不可读取，请稍后刷新。";
            case "memory_queue_unavailable":
                return "记忆队列暂不可用，请稍后刷新。";
            case "memory_processed_read_failed":
                return "已处理记忆暂时不可读取，请稍后刷新。";
            case "memory_processed_unavailable":
                return "已处理记忆列表暂不可用，请稍后刷新。";
            case "memory_note_unavailable":
                return "记忆 note 预览暂不可用，请稍后刷新。";
            case "memory_note_not_found":
                return "未找到对应的记忆 note。";
            case "memory_note_read_failed":
                return "读取记忆 note 失败，请稍后重试。";
            case "memory_note_invalid":
                return "note 内容校验未通过（不能为空）。";
            case "memory_note_update_failed":
                return "记忆 note 修改未成功，请稍后重试。";
            case "memory_index_notes_unavailable":
                return "归并记忆列表暂不可用，请稍后刷新。";
            case "memory_index_notes_read_failed":
                return "归并记忆暂时不可读取，请稍后刷新。";
            case "memory_current_unavailable":
                return "当前记忆列表暂不可用，请稍后刷新。";
            case "memory_current_read_failed":
                return "当前记忆暂时不可读取，请稍后刷新。";
            case "memory_current_invalid_id":
                return "记忆 ID 无效，请刷新列表后重试。";
            case "memory_current_not_found":
                return "未找到对应的记忆，可能已被删除，请刷新列表。";
            case "memory_current_invalid":
                return "记忆内容校验未通过（不能为空或超出长度限制）。";
            case "memory_current_update_failed":
                return "记忆修改未成功，请稍后重试。";
            case "memory_current_delete_failed":
                return "记忆删除未成功，请稍后重试。";
            case "memory_failed_unavailable":
                return "失败记忆列表暂不可用，请稍后刷新。";
            case "memory_failed_read_failed":
                return "失败记忆暂时不可读取，请稍后刷新。";
            case "memory_failed_not_found":
                return "未找到对应的失败记忆记录，可能已被处理，请刷新列表。";
            case "memory_failed_not_parked":
                return "该记录当前不在停车状态（可能已被自动重排队），请刷新列表。";
            case "memory_failed_invalid_id":
                return "失败记忆记录 ID 无效。";
            case "memory_failed_mutation_failed":
                return "失败记忆操作未成功，请稍后重试。";
            case "memory_admin_mutation_disabled":
                return "记忆运维变更未启用：请在服务端设置环境变量 G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS 后重试。";
            case "memory_admin_audit_failed":
                return "操作审计写入失败，本次操作已被拒绝，请稍后重试。";
            case "audit_events_read_failed":
                return "审计事件暂时不可读取，请稍后刷新。";
            case "llm_binding_key_exists":
                return "模型ID已存在，请使用其他模型ID。";
            default:
                return "";
        }
    }

    static _friendlyErrorMessageForText(text) {
        const normalized = String(text || "").trim().toLowerCase();
        if (!normalized) return "";
        if (normalized === "model_keys must not be empty") {
            return "请先为主Agent、执行Agent、检验Agent配置模型链，全部配置完成后才可保存。";
        }
        return "";
    }

    static friendlyErrorMessage(value, fallback = "") {
        const code = this.getErrorCode(value);
        const friendly = this._friendlyErrorMessageForCode(code);
        if (friendly) return friendly;
        if (value && typeof value === "object") {
            const detailText = typeof value.detail === "string" ? value.detail.trim() : "";
            const friendlyDetailText = this._friendlyErrorMessageForText(detailText);
            if (friendlyDetailText) return friendlyDetailText;
            const detailMessage = typeof value.detail?.message === "string" ? value.detail.message.trim() : "";
            const friendlyDetailMessage = this._friendlyErrorMessageForText(detailMessage);
            if (friendlyDetailMessage) return friendlyDetailMessage;
            if (detailMessage) return detailMessage;
            const directMessage = typeof value.message === "string" ? value.message.trim() : "";
            const friendlyDirectMessage = this._friendlyErrorMessageForText(directMessage);
            if (friendlyDirectMessage) return friendlyDirectMessage;
            if (directMessage) return directMessage;
        }
        const friendlyStringValue = this._friendlyErrorMessageForText(value);
        if (friendlyStringValue) return friendlyStringValue;
        if (typeof value === "string" && value.trim()) return value.trim();
        const friendlyFallback = this._friendlyErrorMessageForText(fallback);
        if (friendlyFallback) return friendlyFallback;
        return String(fallback || "").trim() || "未知错误";
    }

    static getActiveSessionId() {
        return String(this._activeSessionId || FALLBACK_SESSION_ID).trim() || FALLBACK_SESSION_ID;
    }

    static setActiveSessionId(sessionId) {
        this._activeSessionId = String(sessionId || "").trim() || FALLBACK_SESSION_ID;
        return this._activeSessionId;
    }

    static _buildUrl(path, params = {}) {
        const base = API_BASE_URL || window.location.origin;
        const url = new URL(path, base);
        Object.entries(params).forEach(([key, value]) => {
            if (value !== undefined && value !== null && value !== "") {
                url.searchParams.set(key, String(value));
            }
        });
        return url;
    }

    static _delay(delayMs) {
        return new Promise((resolve) => window.setTimeout(resolve, Math.max(0, Number(delayMs) || 0)));
    }

    static _isTimeoutError(error) {
        if (!error || typeof error !== "object") return false;
        if (error.isTimeout === true) return true;
        return String(error?.name || "").trim() === "AbortError" && String(error?.message || "").trim() === "Request timeout";
    }

    static async _request(method, path, { params = {}, body, headers = {}, requestKey = "", timeoutMs = 10000 } = {}) {
        const url = this._buildUrl(path, params);
        const requestHeaders = { ...headers };
        const isFormData = body instanceof FormData;
        if (body !== undefined && !isFormData && !requestHeaders["Content-Type"]) {
            requestHeaders["Content-Type"] = "application/json";
        }
        const normalizedKey = String(requestKey || "").trim();
        if (normalizedKey && this._requestControllers.has(normalizedKey)) {
            try {
                this._requestControllers.get(normalizedKey)?.abort();
            } catch (error) {
                void error;
            }
        }
        const controller = new AbortController();
        const token = `${Date.now()}:${Math.random().toString(36).slice(2)}`;
        if (normalizedKey) {
            this._requestControllers.set(normalizedKey, controller);
            this._requestTokens.set(normalizedKey, token);
        }
        // `timeoutMs: 0`（或负数/Infinity）表示**不设客户端超时**。资源删除这类请求的时长
        // 由服务端磁盘决定（MCP 工具目录常带 node_modules，Windows 上 rmtree 能跑几十秒），
        // 客户端掐表只会把已经成功的删除报成 "Request timeout"，用户还得刷新才知道删掉了。
        const timeoutBudget = Number(timeoutMs);
        const timeoutEnabled = Number.isFinite(timeoutBudget) ? timeoutBudget > 0 : timeoutMs === Infinity;
        try {
            let didTimeout = false;
            let timeoutId = 0;
            if (timeoutEnabled) {
                timeoutId = window.setTimeout(() => {
                    didTimeout = true;
                    controller.abort(new DOMException("Request timeout", "AbortError"));
                }, Math.max(1000, timeoutBudget));
            }
            let response;
            try {
                response = await fetch(url.toString(), {
                    method,
                    headers: requestHeaders,
                    body: body === undefined ? undefined : (isFormData ? body : JSON.stringify(body)),
                    signal: controller.signal,
                });
            } catch (error) {
                if (error?.name === "AbortError") {
                    const abortError = new Error(didTimeout ? "Request timeout" : "Request aborted");
                    abortError.name = "AbortError";
                    abortError.isTimeout = didTimeout;
                    throw abortError;
                }
                throw error;
            } finally {
                if (timeoutId) window.clearTimeout(timeoutId);
            }
            if (normalizedKey && this._requestTokens.get(normalizedKey) !== token) {
                const staleError = new Error("Stale request");
                staleError.name = "AbortError";
                throw staleError;
            }
            if (!response.ok) {
                const payload = await response.json().catch(() => ({}));
                const detail = payload.detail !== undefined ? payload.detail : payload.message;
                const fallbackMessage = typeof detail === "string"
                    ? detail
                    : (detail && typeof detail === "object" && typeof detail.message === "string" && detail.message.trim())
                        ? detail.message.trim()
                        : payload.message || `HTTP ${response.status}`;
                const code = this.getErrorCode(detail) || this.getErrorCode(payload);
                const message = this.friendlyErrorMessage({ code, detail, payload, message: fallbackMessage }, fallbackMessage);
                const error = new Error(message);
                error.code = code;
                if (detail && typeof detail === "object") error.data = detail;
                else if (code) error.data = { code, message };
                error.status = response.status;
                error.payload = payload;
                throw error;
            }
            const payload = await response.json();
            if (normalizedKey && this._requestTokens.get(normalizedKey) !== token) {
                const staleError = new Error("Stale request");
                staleError.name = "AbortError";
                throw staleError;
            }
            return payload;
        } finally {
            if (normalizedKey && this._requestControllers.get(normalizedKey) === controller) {
                this._requestControllers.delete(normalizedKey);
                this._requestTokens.delete(normalizedKey);
            }
        }
    }

    static get(path, params = {}) {
        return this._request("GET", path, { params });
    }

    static post(path, body = {}, params = {}) {
        return this._request("POST", path, { params, body });
    }

    static put(path, body = {}, params = {}) {
        return this._request("PUT", path, { params, body });
    }

    static patch(path, body = {}, params = {}) {
        return this._request("PATCH", path, { params, body });
    }

    static delete(path, options = {}) {
        const normalized = options && typeof options === "object" && !Array.isArray(options)
            ? (Object.prototype.hasOwnProperty.call(options, "params")
                || Object.prototype.hasOwnProperty.call(options, "body")
                || Object.prototype.hasOwnProperty.call(options, "headers"))
                ? options
                : { params: options }
            : {};
        return this._request("DELETE", path, normalized);
    }
}

window.ApiCore = ApiCore;
