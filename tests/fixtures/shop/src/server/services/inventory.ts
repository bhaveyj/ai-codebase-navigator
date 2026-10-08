import { stock } from '../db/store';
import type { CartItem } from '../../shared/types';

export function checkInventory(items: CartItem[]): void {
  for (const item of items) {
    if ((stock.get(item.productId) ?? 0) < item.quantity) {
      throw new Error(`Out of stock: ${item.productId}`);
    }
  }
}

export function reserveInventory(items: CartItem[]): void {
  checkInventory(items);
  for (const item of items) {
    stock.set(item.productId, stock.get(item.productId)! - item.quantity);
  }
}
