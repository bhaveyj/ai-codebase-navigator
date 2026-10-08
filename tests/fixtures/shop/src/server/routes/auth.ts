import { Router } from 'express';
import { validateEmail } from '../../shared/validation';
import { findUserByEmail } from '../services/users';
import { createSession, deleteSession } from '../services/sessions';

export const authRouter = Router();

export function login(request: any, response: any): void {
  const email = String(request.body.email ?? '');
  if (!validateEmail(email)) {
    response.status(400).json({ error: 'Enter a valid email address' });
    return;
  }
  const user = findUserByEmail(email);
  if (!user) {
    response.status(404).json({ error: 'Account not found' });
    return;
  }
  // Simplified fixture flow; this is not production authentication.
  const session = createSession(user.id);
  response.json({ user, sessionId: session.id });
}

authRouter.post('/login', login);
authRouter.post('/logout', (request, response) => {
  deleteSession(String(request.body.sessionId));
  response.status(204).end();
});
