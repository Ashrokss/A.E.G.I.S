import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwind from "@tailwindcss/vite";

// Local dev mirrors CloudFront's routing: the SPA is served here, /api and
// /auth/aws go to the api (compose, :3005), /agent-api to the agent (:8000).
export default defineConfig({
  plugins: [react(), tailwind()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:3005",
      "/auth/aws": "http://localhost:3005",
      "/agent-api": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
