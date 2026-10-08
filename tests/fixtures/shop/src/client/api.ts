import type { CartItem, Order, User } from '../shared/types';

export async function request<T>(path: string, body: unknown, sessionId?: string): Promise<T> {
  const response = await fetch(`/api/${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(sessionId ? { Authorization: `Bearer ${sessionId}` } : {}) },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error((await response.json()).error);
  return response.json();
}

export function signIn(email: string) {
  return request<{ user: User; sessionId: string }>('auth/login', { email });
}

export function placeOrder(items: CartItem[], sessionId: string) {
  return request<Order>('checkout', { items }, sessionId);
}
