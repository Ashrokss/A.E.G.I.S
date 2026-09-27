import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwind from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";
import { demoApi } from "./demo/mockApi.ts";

// Normal dev mirrors CloudFront's routing: the SPA is served here, /api and
// /auth/aws go to the api (compose, :3005), /agent-api to the agent (:8000).
//
// `--mode demo` (npm run demo) instead answers every API call from an
// in-memory mock (demo/mockApi.ts) and signs in without Cognito, so the UI can
// be explored with no backend, AWS account or .env.
export default defineConfig(({ mode }) => {
  const demo = mode === "demo";
  return {
    plugins: [react(), tailwind(), demo && demoApi()],
    resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
    define: demo
      ? {
          "import.meta.env.VITE_DEMO": JSON.stringify("1"),
          "import.meta.env.VITE_COGNITO_DOMAIN": JSON.stringify("https://demo.invalid"),
          "import.meta.env.VITE_COGNITO_CLIENT_ID": JSON.stringify("demo"),
        }
      : {},
    server: {
      port: 5173,
      proxy: demo
        ? undefined
        : {
            "/api": "http://localhost:3005",
            "/auth/aws": "http://localhost:3005",
            "/agent-api": { target: "http://localhost:8000", changeOrigin: true },
          },
    },
    preview: { port: 5173, strictPort: true },
  };
});
