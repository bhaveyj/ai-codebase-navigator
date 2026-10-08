import { sessions, nextId } from '../db/store';
import { findUserById } from './users';
import type { Session, User } from '../../shared/types';

export function createSession(userId: string): Session {
  const session = { id: nextId('session'), userId, expiresAt: Date.now() + 3_600_000 };
  sessions.set(session.id, session);
  return session;
}

export function resolveSession(sessionId: string): User | undefined {
  const session = sessions.get(sessionId);
  if (!session || session.expiresAt < Date.now()) return undefined;
  return findUserById(session.userId);
}

export function deleteSession(sessionId: string): void {
  sessions.delete(sessionId);
}
