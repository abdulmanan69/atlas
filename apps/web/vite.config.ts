/// <reference types="vitest/config" />
import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const apiTarget = process.env.ATLAS_API_PROXY ?? "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    port: 5173,
    strictPort: true,
    // Same-origin in dev so the refresh cookie (path /api/v1/auth) just works.
    proxy: { "/api": { target: apiTarget, changeOrigin: false } },
  },
  build: {
    target: "es2022",
    sourcemap: true,
    rollupOptions: {
      output: {
        // Long-lived vendor chunks: app deploys don't invalidate cached libraries.
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (/[\\/]node_modules[\\/](\.pnpm[\\/])?(react|react-dom|scheduler|react-router)[@\\/]/.test(id)) return "react";
          // Route-scoped heavyweights: only the routes that import them pull these in.
          if (/[\\/](ag-grid-community|ag-grid-react|ag-stack|ag-charts-types)[@\\/]/.test(id)) return "grid";
          if (/[\\/](react-day-picker|date-fns|@date-fns)[@\\/+]/.test(id)) return "dates";
          if (/[\\/](@xyflow|@dagrejs)[\\/+]/.test(id)) return "flow";
          if (/recharts|d3-|victory-vendor/.test(id)) return "charts";
          if (/zod|react-hook-form|@hookform/.test(id)) return "forms";
          if (/@radix-ui|@floating-ui|react-remove-scroll|cmdk|aria-hidden/.test(id)) return "ui";
          if (/@tanstack|openapi-fetch/.test(id)) return "data";
          return "vendor";
        },
      },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
  },
});
