import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    watch: {
      ignored: [
        "**/.tmp/**",
        "**/*.tmpdir/**",
        "**/.*.tmpdir/**",
        "**/node_modules/**",
        "**/dist/**",
      ],
    },
    proxy: {
      "/api": {
        target: process.env.AM_API_TARGET || "http://127.0.0.1:8099",
        changeOrigin: true,
      },
    },
  },
  build: { outDir: "dist" },
});
