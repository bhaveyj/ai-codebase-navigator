export interface User {
  id: string;
  email: string;
  name: string;
}

export interface CartItem {
  productId: string;
  quantity: number;
  unitPrice: number;
}

export interface Order {
  id: string;
  userId: string;
  items: CartItem[];
  total: number;
  paymentId: string;
}

export interface Session {
  id: string;
  userId: string;
  expiresAt: number;
}
