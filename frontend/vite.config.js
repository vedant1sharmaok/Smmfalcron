import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],

  build: {
    outDir: "dist",
    emptyOutDir: true,

    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ["react", "react-dom"],
        },
      },
    },

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

  base: "/static/miniapp/",
});
