import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { mockPlugin } from "./src/mock/server";
import path from "node:path";
import { fileURLToPath } from "node:url";

const dirname = path.dirname(fileURLToPath(import.meta.url));

// 开发用 Vite dev server；生产构建产物由后端 FastAPI 静态托管
export default defineConfig({
  // mock 阶段：/api/* 由本进程内存 mock 提供，无真实网络调用
  plugins: [react(), tailwindcss(), mockPlugin()],
  resolve: {
    alias: {
      "@": path.resolve(dirname, "./src"),
    },
  },
});
