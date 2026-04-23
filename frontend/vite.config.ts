import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    host: true,
    // Polling avoids inotify exhaustion when running inside a container with a
    // host-mounted src volume (low max_user_instances on the Podman host).
    watch: {
      usePolling: true,
      interval: 300,
    },
  },
});
