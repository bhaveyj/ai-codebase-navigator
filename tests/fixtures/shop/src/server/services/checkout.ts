import type { CartItem, Order } from '../../shared/types';
import { validateCart, calculateTotal } from '../../shared/validation';
import { checkInventory, reserveInventory } from './inventory';
import { createPayment } from './payments';
import { orders, nextId } from '../db/store';

export async function checkout(userId: string, items: CartItem[]): Promise<Order> {
  validateCart(items);
  checkInventory(items);
  const total = calculateTotal(items);
  const payment = await createPayment(total, userId);
  reserveInventory(items);
  const order = { id: nextId('order'), userId, items, total, paymentId: payment.id };
  orders.set(order.id, order);
  return order;
}
