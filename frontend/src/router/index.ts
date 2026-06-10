/**
 * Vue Router 配置
 */
import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore, useSystemStore } from '@/stores'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    // 登录页
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/Login.vue'),
      meta: { title: '登录', public: true }
    },
    // 注册页
    {
      path: '/register',
      name: 'register',
      component: () => import('@/views/Register.vue'),
      meta: { title: '注册', public: true }
    },
    // 主应用
    {
      path: '/',
      component: () => import('@/layouts/AppLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        {
          path: '',
          name: 'dashboard',
          component: () => import('@/views/Dashboard.vue'),
          meta: { title: '仪表盘' }
        },
        {
          path: 'tasks',
          name: 'task-list',
          component: () => import('@/views/TaskList.vue'),
          meta: { title: '任务列表' }
        },
        {
          path: 'tasks/submit',
          name: 'task-submit',
          component: () => import('@/views/TaskSubmit.vue'),
          meta: { title: '提交任务' }
        },
        {
          path: 'tasks/:id',
          name: 'task-detail',
          component: () => import('@/views/TaskDetail.vue'),
          meta: { title: '任务详情' }
        },
        {
          path: 'queue',
          name: 'queue-management',
          component: () => import('@/views/QueueManagement.vue'),
          meta: { title: '队列管理' }
        },
        {
          path: 'profile',
          name: 'profile',
          component: () => import('@/views/Profile.vue'),
          meta: { title: '个人资料' }
        },
        {
          path: 'users',
          name: 'user-management',
          component: () => import('@/views/UserManagement.vue'),
          meta: { title: '用户管理', requiresAdmin: true }
        },
        {
          path: 'system-config',
          name: 'system-config',
          component: () => import('@/views/SystemConfig.vue'),
          meta: { title: '系统配置', requiresAdmin: true }
        },
        {
          path: 'api-docs',
          name: 'api-docs',
          component: () => import('@/views/ApiDocsScalar.vue'),
          // Scalar 卸载时会自行拆除 DOM,与 AppLayout 的 out-in 过渡冲突导致后续路由空白,
          // 故对该页禁用过渡(详见 AppLayout.vue 的 router-view)
          meta: { title: 'API 文档', noTransition: true }
        }
      ]
    },
  ]
})

// 全局导航守卫
router.beforeEach(async (to, _from, next) => {
  const authStore = useAuthStore()
  const systemStore = useSystemStore()

  // 确保系统配置已加载（用于页面标题）
  if (!systemStore.config.system_name || systemStore.config.system_name === 'MinerU Tianshu') {
    await systemStore.loadConfig()
  }

  // 设置页面标题
  if (to.meta.title) {
    systemStore.updatePageTitle(to.meta.title as string)
  } else {
    document.title = systemStore.config.system_name
  }

  // 🔥 关键修复：如果有 token 但没有用户信息，先初始化
  // 这解决了刷新页面时的竞态条件问题
  if (authStore.token && !authStore.user) {
    await authStore.initialize()
  }

  // 公开页面（登录、注册）
  if (to.meta.public) {
    // 如果已登录，重定向到首页
    if (authStore.isAuthenticated) {
      next('/')
    } else {
      next()
    }
    return
  }

  // 需要认证的页面
  if (to.meta.requiresAuth || to.matched.some(record => record.meta.requiresAuth)) {
    if (!authStore.isAuthenticated) {
      // 未登录，重定向到登录页
      next({
        path: '/login',
        query: { redirect: to.fullPath }
      })
      return
    }

    // 检查是否需要管理员权限
    if (to.meta.requiresAdmin && !authStore.isAdmin) {
      // 非管理员无权访问
      next('/')
      return
    }

    next()
  } else {
    next()
  }
})

export default router
