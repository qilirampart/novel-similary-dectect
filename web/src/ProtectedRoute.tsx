import { Navigate, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "./auth";

export function ProtectedRoute() {
  const location = useLocation();
  const { user, loading } = useAuth();

  if (loading) {
    return <div className="app-loading-screen">正在校验登录状态...</div>;
  }

  if (!user) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return <Outlet />;
}
