import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { cpSync } from "node:fs";
import { resolve } from "node:path";

// PDF.js loads character maps (CJK text), standard fonts and its WebAssembly image decoders at runtime.
const pdfjsAssets = (): Plugin => ({
  name: "pdfjs-assets",
  writeBundle(opts) {
    const out = resolve(opts.dir ?? "dist", "pdfjs");
    for (const d of ["cmaps", "standard_fonts", "wasm"]) cpSync(resolve("node_modules/pdfjs-dist", d), resolve(out, d), { recursive: true });
  },
});

// Production assets are served by the FastAPI server from frontend/dist (same origin).
// `npm run dev` proxies API and WebSocket traffic to a running backend on 127.0.0.1:8765.
export default defineConfig({
  plugins: [react(), pdfjsAssets()],
  // Monaco's ESM files by path (its package "exports" do not expose the CSS and per-feature modules)
  resolve: { alias: { "monaco-esm": resolve("node_modules/monaco-editor/esm/vs") } },
  build: {
    outDir: "dist",
    sourcemap: false,
    target: "es2022", // PDF.js uses ES2022 (private fields, top-level await)
    rollupOptions: {
      output: {
        manualChunks: {
          xterm: ["@xterm/xterm", "@xterm/addon-fit"],
          markdown: ["markdown-it", "dompurify"],
          react: ["react", "react-dom"],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: false, ws: true },
    },
  },
});
