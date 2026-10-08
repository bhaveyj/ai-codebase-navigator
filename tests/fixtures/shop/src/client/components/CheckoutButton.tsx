import { useState } from 'react';
import { placeOrder } from '../api';
import { calculateTotal } from '../../shared/validation';
import type { CartItem } from '../../shared/types';

export function CheckoutButton({ items, sessionId }: { items: CartItem[]; sessionId: string }) {
  const [status, setStatus] = useState('');
  const total = calculateTotal(items);

  async function submit() {
    setStatus('Placing your order…');
    try {
      const order = await placeOrder(items, sessionId);
      setStatus(`Order ${order.id} confirmed`);
    } catch (error) {
      setStatus((error as Error).message);
    }
  }

  return <section>
    <button onClick={submit}>Checkout · {total.toFixed(2)}</button>
    <p aria-live="polite">{status}</p>
  </section>;
}
