import { users } from '../db/store';
import type { User } from '../../shared/types';

export function findUserByEmail(email: string): User | undefined {
  return [...users.values()].find(user => user.email === email.toLowerCase());
}

export function findUserById(id: string): User | undefined {
  return users.get(id);
}
