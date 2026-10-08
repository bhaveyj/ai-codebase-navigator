import { useState } from 'react';
import { LoginForm } from './components/LoginForm';
import { CheckoutButton } from './components/CheckoutButton';

export default function App() {
  const [sessionId, setSessionId] = useState('');
  const items = [{ productId: 'notebook', quantity: 1, unitPrice: 12 }];
  return <main>
    <h1>Beacon Store</h1>
    {sessionId
      ? <CheckoutButton items={items} sessionId={sessionId} />
      : <LoginForm onSession={setSessionId} />}
  </main>;
}
