const windows = new Map<string, { count: number; resetsAt: number }>();
const WINDOW_MS = 60_000;
const MAX_REQUESTS = 30;

export function rateLimit(request: any, response: any, next: () => void): void {
  const key = request.ip;
  const now = Date.now();
  let window = windows.get(key);
  if (!window || now >= window.resetsAt) {
    window = { count: 0, resetsAt: now + WINDOW_MS };
    windows.set(key, window);
  }
  window.count += 1;
  if (window.count > MAX_REQUESTS) {
    response.setHeader('Retry-After', Math.ceil((window.resetsAt - now) / 1000));
    response.status(429).json({ error: 'Too many requests' });
    return;
  }
  next();
}
