import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      // 新架构：Python PTY WebSocket 直连
      "/ws": {
        target: "http://localhost:8000",
        changeOrigin: true,
        ws: true,
      },
      // WeTTY 反代（过渡期保留）
      "/wetty": {
        target: "http://localhost:8000",
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
