import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'
import tailwindcss from '@tailwindcss/vite'
import { monacoWorkers } from './build/monacoWorkers'

// Vue DevTools makes Pinia deep-watch every store synchronously, which stalls
// busy chats with many subagents. Opt in with VITE_VUE_DEVTOOLS=true.
const enableVueDevTools = process.env.VITE_VUE_DEVTOOLS === 'true'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue(), monacoWorkers(), ...(enableVueDevTools ? [vueDevTools()] : []), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/ws/runner': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        ws: true,
      },
      '/ws/desktop': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
