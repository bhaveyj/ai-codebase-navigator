import { Router } from 'express';
import { requireAuth } from '../middleware/auth';
import { checkout } from '@/server/services/checkout';

export const checkoutRouter = Router();

export async function submitOrder(request: any, response: any) {
  try {
    const order = await checkout(request.user.id, request.body.items);
    response.status(201).json(order);
  } catch (error) {
    response.status(400).json({ error: (error as Error).message });
  }
}

checkoutRouter.post('/', requireAuth, submitOrder);
