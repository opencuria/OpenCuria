import { defineConfig, mergeConfig } from 'vite'
import base from './vite.config.ts'
export default mergeConfig(
  base,
  defineConfig({
    server: {
      port: 5177,
      strictPort: true,
      proxy: {
        '/api': { target: 'http://127.0.0.1:18081', changeOrigin: true },
        '/ws/runner': { target: 'http://127.0.0.1:18081', changeOrigin: true, ws: true },
        '/ws/desktop': { target: 'http://127.0.0.1:18081', changeOrigin: true, ws: true },
      },
    },
  }),
)
