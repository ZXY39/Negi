/**
 * 通用 HTTP 客户端封装
 *
 * 提供带超时和错误处理的 HTTP 请求功能
 */

/**
 * HTTP 请求选项
 */
export interface HttpRequestOptions {
  /** 请求超时时间（毫秒），默认 30000 */
  timeout?: number;
  /** 请求头 */
  headers?: Record<string, string>;
}

/**
 * HTTP 错误
 */
export class HttpError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly body?: string
  ) {
    super(message);
    this.name = "HttpError";
  }
}

/**
 * 超时错误
 */
export class TimeoutError extends Error {
  constructor(message: string, public readonly timeoutMs: number) {
    super(message);
    this.name = "TimeoutError";
  }
}

/**
 * 发送 HTTP POST 请求
 *
 * @param url 请求 URL
 * @param body 请求体
 * @param options 请求选项
 * @returns 响应数据
 *
 * @example
 * ```ts
 * const data = await httpPost("https://api.example.com/token", { key: "value" }, { timeout: 10000 });
 * ```
 */
export async function httpPost<T = unknown>(
  url: string,
  body: unknown,
  options?: HttpRequestOptions
): Promise<T> {
  const { timeout = 30000, headers = {} } = options ?? {};

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...headers,
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });

    if (!response.ok) {
      const responseBody = await response.text().catch(() => "");
      throw new HttpError(
        `HTTP ${response.status}: ${response.statusText}`,
        response.status,
        responseBody
      );
    }

    return (await response.json()) as T;
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      throw new TimeoutError(`Request timeout after ${timeout}ms`, timeout);
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * 发送 HTTP GET 请求
 *
 * @param url 请求 URL
 * @param options 请求选项
 * @returns 响应数据
 *
 * @example
 * ```ts
 * const data = await httpGet("https://api.example.com/data", { timeout: 10000 });
 * ```
 */
export async function httpGet<T = unknown>(
  url: string,
  options?: HttpRequestOptions
): Promise<T> {
  const { timeout = 30000, headers = {} } = options ?? {};

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);

  try {
    const response = await fetch(url, {
      method: "GET",
      headers,
      signal: controller.signal,
    });

    if (!response.ok) {
      const responseBody = await response.text().catch(() => "");
      throw new HttpError(
        `HTTP ${response.status}: ${response.statusText}`,
        response.status,
        responseBody
      );
    }

    return (await response.json()) as T;
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      throw new TimeoutError(`Request timeout after ${timeout}ms`, timeout);
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}
