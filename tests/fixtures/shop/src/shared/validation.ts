import type { CartItem } from './types';

export function validateEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

export function validateCart(items: CartItem[]): void {
  if (items.length === 0) throw new Error('Your cart is empty');
  for (const item of items) {
    if (!Number.isInteger(item.quantity) || item.quantity < 1) {
      throw new Error('Quantity must be a positive integer');
    }
    if (item.unitPrice < 0) throw new Error('Invalid product price');
  }
}

export function calculateTotal(items: CartItem[]): number {
  return items.reduce((sum, item) => sum + item.unitPrice * item.quantity, 0);
}
