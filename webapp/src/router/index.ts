import { createRouter, createWebHistory } from 'vue-router'
import AppLayout from '@/components/layout/AppLayout.vue'
import { useAuthStore } from '@/stores/auth'

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
          name: 'dashboard',
          component: () => import('@/views/DashboardView.vue'),
          meta: { title: 'Dashboard' },
        },
        {
          path: 'runners',
          name: 'runners',
          component: () => import('@/views/RunnersView.vue'),
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
          meta: { title: 'Workspace', hideTopBar: true },
        },
        {
          path: 'images',
          name: 'images',
          component: () => import('@/views/ImagesView.vue'),
          meta: { title: 'Captured Images' },
        },
        {
          path: 'skills',
          name: 'skills',
          component: () => import('@/views/SkillsView.vue'),
          meta: { title: 'Skills' },
        },
        {
          path: 'credentials',
          name: 'credentials',
          component: () => import('@/views/CredentialsView.vue'),
          meta: { title: 'Credentials' },
        },
        {
          path: 'api-keys',
          name: 'api-keys',
          component: () => import('@/views/ApiKeysView.vue'),
          meta: { title: 'API Keys' },
        },
        {
          path: 'org-settings',
          name: 'org-settings',
          component: () => import('@/views/OrgSettingsView.vue'),
          meta: { title: 'Organization Settings' },
        },
        {
          path: 'docs/:slug(.*)*',
          name: 'docs-detail',
          component: () => import('@/views/DocsView.vue'),
          meta: { title: 'Docs' },
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
