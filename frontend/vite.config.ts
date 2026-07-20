import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      "/api": "http://localhost:8000",
      // Keep development uploads same-origin too. The host must remain identical
      // to the one used to sign MinIO URLs in the WSL API process.
      "/storage": {
        target: "http://127.0.0.1:9000",
        changeOrigin: true,
        rewrite: path => path.replace(/^\/storage/, ""),
      },
    },
  },
});
