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
