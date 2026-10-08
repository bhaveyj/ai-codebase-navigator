import { resolveSession } from '../services/sessions';

export function requireAuth(request: any, response: any, next: () => void): void {
  const token = request.headers.authorization?.replace('Bearer ', '');
  const user = token ? resolveSession(token) : undefined;
  if (!user) {
    response.status(401).json({ error: 'Please sign in' });
    return;
  }
  request.user = user;
  next();
}
