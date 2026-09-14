import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const appBasePath = process.env.VITE_APP_BASE_PATH || "/";

export default defineConfig({
  base: appBasePath,
  plugins: [react()],
  build: {
    emptyOutDir: true
  },
  server: {
    host: "127.0.0.1",
    port: 4175,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8001",
        changeOrigin: true
      }
    }
  }
});
