import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { AuthProvider } from "./auth";
import "./styles.css";

function resolveRouterBase(): string | undefined {
  const rawBase = import.meta.env.BASE_URL || "/";
  if (!rawBase || rawBase === "/") {
    return undefined;
  }
  return rawBase.endsWith("/") ? rawBase.slice(0, -1) : rawBase;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthProvider>
      <BrowserRouter
        basename={resolveRouterBase()}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <App />
      </BrowserRouter>
    </AuthProvider>
  </React.StrictMode>
);
