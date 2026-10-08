import type { User, Session, Order } from '../../shared/types';

// In-memory persistence keeps this fixture small and readable.
export const users = new Map<string, User>([
  ['user-1', { id: 'user-1', email: 'alex@example.test', name: 'Alex' }],
]);
export const sessions = new Map<string, Session>();
export const orders = new Map<string, Order>();
export const stock = new Map<string, number>([['notebook', 50], ['pencil', 100]]);

export function nextId(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}
