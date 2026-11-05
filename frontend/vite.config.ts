import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Production assets are served by the FastAPI server from frontend/dist (same origin).
// `npm run dev` proxies API and WebSocket traffic to a running backend on 127.0.0.1:8765.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false, target: "es2020" },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: false, ws: true },
    },
  },
});
