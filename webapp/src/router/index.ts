import { createRouter, createWebHistory } from 'vue-router'
import AppLayout from '@/components/layout/AppLayout.vue'
import { useAuthStore } from '@/stores/auth'
import { resolveSettingsTab } from '@/components/settings/settingsTabs'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    // ---- Public routes (no auth required) ----
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { guest: true },
    },
    {
      path: '/register',
      redirect: '/login',
    },
    {
      path: '/sso/callback',
      name: 'sso-callback',
      component: () => import('@/views/SsoCallbackView.vue'),
      meta: { guest: true },
    },
    {
      path: '/create-organization',
      name: 'create-organization',
      component: () => import('@/views/CreateOrganizationView.vue'),
      meta: { requiresAuth: true },
    },

    // ---- Authenticated routes (inside AppLayout) ----
    {
      path: '/',
      component: AppLayout,
      meta: { requiresAuth: true, requiresOrg: true },
      children: [
        {
          path: '',
          name: 'home',
          component: () => import('@/views/ChatHomeView.vue'),
          meta: { title: 'New Chat', fullBleed: true },
        },
        {
          path: 'runners',
          name: 'runners',
          redirect: { path: '/', query: { settings: 'runners' } },
          meta: { title: 'Runners' },
        },
        {
          path: 'workspaces',
          name: 'workspaces',
          component: () => import('@/views/WorkspacesView.vue'),
          meta: { title: 'Workspaces' },
        },
        {
          path: 'workspaces/:id',
          name: 'workspace-detail',
          component: () => import('@/views/WorkspaceDetailView.vue'),
          meta: { title: 'Workspace', hideTopBar: true, fullBleed: true },
        },
        {
          path: 'images',
          name: 'images',
          redirect: { path: '/', query: { settings: 'images' } },
          meta: { title: 'Captured Images' },
        },
        {
          path: 'skills',
          name: 'skills',
          redirect: { path: '/', query: { settings: 'skills' } },
          meta: { title: 'Skills' },
        },
        {
          path: 'credentials',
          name: 'credentials',
          redirect: { path: '/', query: { settings: 'credentials' } },
          meta: { title: 'Credentials' },
        },
        {
          path: 'api-keys',
          name: 'api-keys',
          redirect: { path: '/', query: { settings: 'api-keys' } },
          meta: { title: 'API Keys' },
        },
        {
          path: 'org-settings',
          name: 'org-settings',
          redirect: (to) => ({
            path: '/',
            query: { settings: resolveSettingsTab(to.query.tab) },
          }),
          meta: { title: 'Organization Settings' },
        },
        {
          path: 'docs/:slug(.*)*',
          name: 'docs-detail',
          component: () => import('@/views/DocsView.vue'),
          meta: { title: 'Docs', fullBleed: true },
        },
      ],
    },
  ],
})

router.beforeEach(async (to) => {
  const authStore = useAuthStore()

  if (!authStore.initialized) {
    await authStore.initialize()
  }

  const requiresAuth = to.matched.some((r) => r.meta.requiresAuth)
  const requiresOrg = to.matched.some((r) => r.meta.requiresOrg)
  const isGuestOnly = to.meta.guest === true

  // Users with existing organizations should never see create-organization page
  if (to.name === 'create-organization' && authStore.isAuthenticated && authStore.hasOrganizations) {
    return '/'
  }

  // If authenticated and visiting a guest-only page → redirect home
  if (isGuestOnly && authStore.isAuthenticated) {
    return authStore.hasOrganizations ? '/' : '/create-organization'
  }

  // If route requires auth and user is not authenticated → login
  if (requiresAuth && !authStore.isAuthenticated) {
    return '/login'
  }

  // If route requires an active org and user has none → create one.
  // Skip this redirect if we have an activeOrganizationId from localStorage —
  // that means the user had an org before but fetchMe() may have failed to
  // reload the list (e.g. transient network error on page reload).
  if (
    requiresOrg &&
    authStore.isAuthenticated &&
    !authStore.hasOrganizations &&
    !authStore.activeOrganizationId
  ) {
    return '/create-organization'
  }
})

export default router
