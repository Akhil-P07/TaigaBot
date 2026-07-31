import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Builds into frontend/dist, which the bot's aiohttp server serves directly
// (see keep_alive.py). During `npm run dev` the API is proxied to the running
// bot on :8080 so login and data work against real state.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/assets-bot': {
        target: 'http://127.0.0.1:8080',
        rewrite: (p) => p.replace(/^\/assets-bot/, '/assets'),
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Bundles go to dist/static/, NOT dist/assets/ — the bot already serves its
    // own /assets (logos, favicon) and the two would collide.
    assetsDir: 'static',
    // No inline modulepreload polyfill, so the CSP can forbid inline scripts.
    modulePreload: { polyfill: false },
  },
})
