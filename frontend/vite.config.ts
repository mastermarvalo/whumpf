import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    // Reaching out through Podman / NPM needs an explicit host binding;
    // the Dockerfile CMD also passes --host 0.0.0.0 which overrides this.
    host: true,
  },
});
