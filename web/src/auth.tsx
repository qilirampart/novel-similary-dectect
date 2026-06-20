import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

import { getCurrentUser, login, logout, type UserProfile } from "./api";

type AuthContextValue = {
  user: UserProfile | null;
  loading: boolean;
  loginAction: (username: string, password: string) => Promise<void>;
  logoutAction: () => Promise<void>;
  refreshUser: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);

  async function refreshUser() {
    try {
      const response = await getCurrentUser();
      setUser(response.user);
    } catch {
      setUser(null);
    }
  }

  async function loginAction(username: string, password: string) {
    const response = await login(username, password);
    setUser(response.user);
  }

  async function logoutAction() {
    try {
      await logout();
    } finally {
      setUser(null);
    }
  }

  useEffect(() => {
    let mounted = true;
    async function bootstrap() {
      try {
        const response = await getCurrentUser();
        if (!mounted) return;
        setUser(response.user);
      } catch {
        if (!mounted) return;
        setUser(null);
      } finally {
        if (mounted) {
          setLoading(false);
        }
      }
    }
    void bootstrap();
    return () => {
      mounted = false;
    };
  }, []);

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        loginAction,
        logoutAction,
        refreshUser
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return context;
}
