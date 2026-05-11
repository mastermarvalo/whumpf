import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiOrigin     = safeOrigin(env.VITE_API_URL,     "http://localhost:8000");
  const titilerOrigin = safeOrigin(env.VITE_TITILER_URL, "http://localhost:8001");

  return {
    plugins: [
      react(),
      VitePWA({
        registerType: "autoUpdate",
        // Inject the registration script into index.html so we don't need a
        // manual `registerSW()` call from app code.
        injectRegister: "auto",
        manifest: {
          name: "Whumpf",
          short_name: "whumpf",
          description: "Backcountry terrain intelligence",
          start_url: "/",
          display: "standalone",
          background_color: "#0d1117",
          theme_color: "#0d1117",
          icons: [
            { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
            { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
            { src: "/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
          ],
        },
        workbox: {
          // The main JS chunk is ~1.3MB; bump the precache limit so we don't
          // fail the build. (Code-splitting later would let us drop this.)
          maximumFileSizeToCacheInBytes: 5 * 1024 * 1024,
          // Runtime caching: cache-first for tiles so users keep working when
          // they head into spotty backcountry coverage. Each tier is bounded
          // by max entries (LRU) so storage doesn't grow unbounded.
          runtimeCaching: [
            {
              urlPattern: ({ url }) =>
                url.origin === apiOrigin && url.pathname.startsWith("/tiles/"),
              handler: "CacheFirst",
              options: {
                cacheName: "whumpf-tiles",
                expiration: { maxEntries: 500, maxAgeSeconds: 30 * 86400 },
                cacheableResponse: { statuses: [0, 200] },
              },
            },
            {
              urlPattern: ({ url }) => url.origin === titilerOrigin,
              handler: "CacheFirst",
              options: {
                cacheName: "titiler-tiles",
                expiration: { maxEntries: 500, maxAgeSeconds: 30 * 86400 },
                cacheableResponse: { statuses: [0, 200] },
              },
            },
            {
              urlPattern: /^https:\/\/server\.arcgisonline\.com\//,
              handler: "CacheFirst",
              options: {
                cacheName: "esri-basemaps",
                expiration: { maxEntries: 500, maxAgeSeconds: 7 * 86400 },
                cacheableResponse: { statuses: [0, 200] },
              },
            },
            {
              urlPattern: /^https:\/\/(tiles\.openfreemap|fonts\.openmaptiles)\./,
              handler: "CacheFirst",
              options: {
                cacheName: "openfreemap",
                expiration: { maxEntries: 200, maxAgeSeconds: 30 * 86400 },
                cacheableResponse: { statuses: [0, 200] },
              },
            },
            {
              urlPattern: /^https:\/\/basemaps\.cartocdn\.com\//,
              handler: "CacheFirst",
              options: {
                cacheName: "carto-basemaps",
                expiration: { maxEntries: 200, maxAgeSeconds: 30 * 86400 },
                cacheableResponse: { statuses: [0, 200] },
              },
            },
          ],
        },
      }),
    ],
    server: {
      port: 5173,
      strictPort: true,
      host: true,
      allowedHosts: ["whumpf.co"],
      // Polling avoids inotify exhaustion when running inside a container with a
      // host-mounted src volume (low max_user_instances on the Podman host).
      watch: {
        usePolling: true,
        interval: 300,
      },
    },
  };
});

function safeOrigin(value: string | undefined, fallback: string): string {
  try {
    return new URL(value ?? fallback).origin;
  } catch {
    return new URL(fallback).origin;
  }
}
