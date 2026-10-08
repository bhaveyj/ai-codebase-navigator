import { nextId } from '../db/store';

// This fixture models the payment boundary without making network requests.
export async function createPayment(total: number, userId: string) {
  if (!Number.isFinite(total) || total <= 0) throw new Error('Invalid payment total');
  return { id: nextId('payment'), total, userId, status: 'authorized' };
}
