import { createApp } from 'vue'
import { createPinia } from 'pinia'

import App from './App.vue'
import router from './router'

import '@/assets/main.css'

import { useAuthStore } from '@/stores/auth'
import { connect as connectSocket } from '@/services/socket'
import { loadConfig } from '@/services/config'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)

// Load runtime config first (fetches /config.json in production),
// then initialize auth and mount the app.
loadConfig().then(() => {
  const authStore = useAuthStore(pinia)
  authStore.initialize().then(() => {
    app.mount('#app')

    // Connect Socket.IO only when authenticated
    if (authStore.isAuthenticated) {
      connectSocket()
    }
  })
})
