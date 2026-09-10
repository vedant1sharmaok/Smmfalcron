import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    // FastAPI serves /static/miniapp — built assets land here directly.
    outDir:    "dist",
    emptyOutDir: true,
    // Chunk strategy: vendor split for better caching.
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ["react", "react-dom"],
        },
      },
    },
    // Telegram Mini Apps run in an iframe; inline all critical CSS.
    cssCodeSplit: false,
    sourcemap: false,
    minify: "esbuild",
    target: "es2020",
  },
  server: {
    port: 5173,
    proxy: {
      "/miniapp/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  // Base path served at root in Telegram's iframe.
  base: "/",
});
