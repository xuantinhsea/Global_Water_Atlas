import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies the API to the local FastAPI process so the browser
// only ever talks to one origin. `dist/` is served by FastAPI in production.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/healthz": "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  worker: {
    format: "es",
  },
});
