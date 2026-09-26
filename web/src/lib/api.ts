// Thin fetch wrapper for the workspace API. Every request sends credentials
// so the session cookie set by /api/auth/login is included, and every
// response is parsed as JSON with a consistent error shape.

export class ApiError extends Error {
  status: number
  code?: string
  detail?: unknown

  constructor(message: string, status: number, code?: string, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }
}

export interface ApiErrorBody {
  error?: {
    code?: string
    message?: string
    details?: unknown
  }
}

let csrfToken: string | null = null

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return undefined
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  const { headers: suppliedHeaders, ...requestInit } = init
  const headers = new Headers(suppliedHeaders)
  headers.set('Accept', 'application/json')
  if (init.body && !(init.body instanceof FormData) && !(init.body instanceof Blob) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (init.method && !['GET', 'HEAD', 'OPTIONS'].includes(init.method.toUpperCase()) && csrfToken) {
    headers.set('X-CSRF-Token', csrfToken)
  }

  try {
    response = await fetch(path, {
      credentials: 'include',
      ...requestInit,
      headers,
    })
  } catch (cause) {
    throw new ApiError(
      'Could not reach the workspace API. Check that the backend is running.',
      0,
      'network_error',
      cause,
    )
  }

  const body = await parseBody(response)

  if (!response.ok) {
    const errBody = (body ?? {}) as ApiErrorBody
    const error = errBody.error
    throw new ApiError(
      error?.message ?? `Request failed with status ${response.status}`,
      response.status,
      error?.code,
      error?.details,
    )
  }

  if (
    body &&
    typeof body === 'object' &&
    'csrf_token' in body &&
    typeof body.csrf_token === 'string'
  ) {
    csrfToken = body.csrf_token
  }
  if (body && typeof body === 'object' && 'authenticated' in body && body.authenticated === false) csrfToken = null
  return body as T
}

export function getCsrfToken() { return csrfToken }

export const api = {
  upload: <T>(path: string, body: FormData | Blob, method = 'POST') => request<T>(path, { method, body }),
  get: <T>(path: string) => request<T>(path, { method: 'GET' }),
  post: <T>(path: string, data?: unknown) =>
    request<T>(path, { method: 'POST', body: data !== undefined ? JSON.stringify(data) : undefined }),
  patch: <T>(path: string, data?: unknown) =>
    request<T>(path, { method: 'PATCH', body: data !== undefined ? JSON.stringify(data) : undefined }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}
