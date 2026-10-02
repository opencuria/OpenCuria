// Vite config consumed by the test-only e2e web server.
export default {
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8001', changeOrigin: true },
      '/ws/runner': { target: 'ws://127.0.0.1:8001', changeOrigin: true, ws: true },
      '/ws/desktop': { target: 'ws://127.0.0.1:8001', changeOrigin: true, ws: true },
    },
  },
}
