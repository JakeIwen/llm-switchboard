// The page's JSON calls. The API stays at the server root even when
// SWITCHBOARD_BASE mounts a development build elsewhere.

export class ApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function parse<T>(resp: Response): Promise<T> {
  const text = await resp.text()
  let body: unknown = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    // A proxy error page or an empty reply: report the status instead.
  }
  if (!resp.ok) {
    const message = (body as { error?: string } | null)?.error ?? `${resp.status} ${resp.statusText}`
    throw new ApiError(resp.status, message)
  }
  return body as T
}

export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return parse<T>(await fetch(path, { signal, cache: 'no-store' }))
}

export async function postJson<T>(path: string, body: object = {}): Promise<T> {
  return parse<T>(
    await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  )
}
