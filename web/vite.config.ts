import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev loop: Vite serves the UI on :5173 and proxies /api to the FastAPI
// service on :8000 (same-origin in the browser, so CORS stays a backstop,
// not a requirement). Production build is served by FastAPI StaticFiles
// from web/dist (multi-stage Dockerfile.web) — no proxy involved.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: process.env.WC_API_URL || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
