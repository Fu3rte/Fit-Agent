import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// 开发用 Vite dev server；生产构建产物由后端 FastAPI 静态托管
// F6-02d：src/mock/ 与 mockPlugin 已删除——开发态 /api/* 由真实后端提供
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
    },
  },
});
