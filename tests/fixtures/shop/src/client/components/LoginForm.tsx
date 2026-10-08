import { useState } from 'react';
import { signIn } from '../api';

export function LoginForm({ onSession }: { onSession: (id: string) => void }) {
  const [email, setEmail] = useState('');
  const [error, setError] = useState('');

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    try {
      const result = await signIn(email);
      onSession(result.sessionId);
    } catch (error) {
      setError((error as Error).message);
    }
  }

  return <form onSubmit={submit}>
    <label>Email<input value={email} onChange={event => setEmail(event.target.value)} /></label>
    <button type="submit">Sign in</button>
    {error && <p role="alert">{error}</p>}
  </form>;
}
