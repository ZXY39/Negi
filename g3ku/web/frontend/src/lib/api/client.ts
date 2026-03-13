const API_PREFIX = '/api'

export class ApiRequestError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

async function parseResponse<T>(response: Response): Promise<T> {
  const contentType = response.headers.get('content-type') || ''
  const payload = contentType.includes('application/json') ? await response.json() : await response.text()
  if (!response.ok) {
    const detail = typeof payload === 'string' ? payload : String(payload?.detail || response.statusText)
    throw new ApiRequestError(response.status, detail)
  }
  return payload as T
}

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_PREFIX}${path}`, {
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
    ...init,
  })
  return parseResponse<T>(response)
}

export function getJson<T>(path: string): Promise<T> {
  return apiRequest<T>(path)
}

export function postJson<T>(path: string, body?: unknown): Promise<T> {
  return apiRequest<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
}

export function putJson<T>(path: string, body?: unknown): Promise<T> {
  return apiRequest<T>(path, { method: 'PUT', body: body === undefined ? undefined : JSON.stringify(body) })
}

export function deleteJson<T>(path: string): Promise<T> {
  return apiRequest<T>(path, { method: 'DELETE' })
}
