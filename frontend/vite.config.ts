import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

const portValue = process.env.FIT_AGENT_FRONTEND_PORT ?? "5173";
const frontendPort = Number(portValue);
if (!/^[0-9]+$/.test(portValue) || frontendPort < 1 || frontendPort > 65535) {
  throw new Error("FIT_AGENT_FRONTEND_PORT 必须是 1–65535 的整数");
}

// 开发用 Vite dev server；生产构建产物由后端 FastAPI 静态托管
// dev server 把 /api 反代到本地后端，浏览器始终同源，不需要 CORS 头。
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
  server: {
    port: frontendPort,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
