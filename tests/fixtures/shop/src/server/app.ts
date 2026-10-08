import express from 'express';
import { authRouter } from './routes/auth';
import { checkoutRouter } from './routes/checkout';
import { rateLimit } from './middleware/rate-limit';

export const app = express();
app.use(express.json());
app.use(rateLimit);
app.use('/api/auth', authRouter);
app.use('/api/checkout', checkoutRouter);
app.get('/api/health', (_request, response) => response.json({ status: 'ok' }));
