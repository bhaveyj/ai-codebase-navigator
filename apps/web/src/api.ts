export class ApiError extends Error { constructor(public status: number, message: string, public code?: string, public retryAfter?: number) { super(message); } }
export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  let response: Response;
  try { response = await fetch(`/api/v1${path}`, { ...options, headers: {'Content-Type': 'application/json', ...options?.headers} }); }
  catch { throw new ApiError(0, 'The API is not reachable. Start the backend and try again.'); }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const retry = Number(response.headers.get('Retry-After'));
    throw new ApiError(response.status, data.error?.message || data.detail || `Request failed (${response.status})`, data.error?.code, Number.isFinite(retry) && retry > 0 ? retry : undefined);
  }
  return response.status === 204 ? undefined as T : response.json();
}
export const post = <T>(path: string, body?: unknown) => api<T>(path, {method: 'POST', body: body === undefined ? undefined : JSON.stringify(body)});
export const enc = encodeURIComponent;

export async function streamPost<T extends {type: string}>(path: string, body: unknown, onEvent: (event: T) => void): Promise<void> {
  let response: Response;
  try { response = await fetch(`/api/v1${path}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}); }
  catch { throw new ApiError(0, 'The API is not reachable. Start the backend and try again.'); }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const retry = Number(response.headers.get('Retry-After'));
    throw new ApiError(response.status, data.error?.message || data.detail || `Request failed (${response.status})`, data.error?.code, Number.isFinite(retry) && retry > 0 ? retry : undefined);
  }
  if (!response.body) throw new ApiError(0, 'The answer stream is unavailable. Try again.');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = ''; let completed = false;
  try {
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value, {stream: !done}).replace(/\r\n/g, '\n');
      let end: number;
      while ((end = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
        const data = frame.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        if (!data) continue;
        const event = JSON.parse(data) as T & {message?: string; retryAfter?: number};
        if (event.type === 'error') throw new ApiError(0, event.message || 'Unable to answer this question.', undefined, event.retryAfter);
        onEvent(event);
        if (event.type === 'done') completed = true;
      }
      if (done) break;
    }
  } finally { reader.releaseLock(); }
  if (!completed) throw new ApiError(0, 'The answer stream ended early. Try again.');
}
