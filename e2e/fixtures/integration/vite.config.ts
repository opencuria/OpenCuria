// Opt-in integration frontend, no changes to the regular development proxy.
// Resolve Vite from webapp; package.json keeps this external fixture ESM.
import { defineConfig, mergeConfig } from '../../../webapp/node_modules/vite/dist/node/index.js'
import base from '../../../webapp/vite.config'

export default mergeConfig(base, defineConfig({
  server: {
    port: 5177,
    strictPort: true,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8011', changeOrigin: true },
      '/ws/runner': { target: 'http://127.0.0.1:8011', changeOrigin: true, ws: true },
      '/ws/desktop': { target: 'http://127.0.0.1:8011', changeOrigin: true, ws: true },
    },
  },
}))
