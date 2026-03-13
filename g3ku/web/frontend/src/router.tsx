import { createBrowserRouter } from 'react-router-dom'

import { AppShell } from '@/layout/AppShell'
import { CeoPage } from '@/pages/CeoPage'
import { ModelsPage } from '@/pages/ModelsPage'
import { SkillsPage } from '@/pages/SkillsPage'
import { TaskDetailPage } from '@/pages/TaskDetailPage'
import { TasksPage } from '@/pages/TasksPage'
import { ToolsPage } from '@/pages/ToolsPage'

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <CeoPage /> },
      { path: 'tasks', element: <TasksPage /> },
      { path: 'tasks/:taskId', element: <TaskDetailPage /> },
      { path: 'skills', element: <SkillsPage /> },
      { path: 'tools', element: <ToolsPage /> },
      { path: 'models', element: <ModelsPage /> },
    ],
  },
])
