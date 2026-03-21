import { Navigate, createBrowserRouter } from 'react-router-dom'

import { AppShell } from '@/app/layouts/AppShell'
import { CurrentTaskPage } from '@/routes/current-task/CurrentTaskPage'
import { MyProjectsPage } from '@/routes/my-projects/MyProjectsPage'
import { NewTranslationPage } from '@/routes/new-translation/NewTranslationPage'
import { NotFoundPage } from '@/routes/not-found/NotFoundPage'
import { ProjectDetailPage } from '@/routes/project-detail/ProjectDetailPage'
import { SpeakerReviewPage } from '@/routes/review/SpeakerReviewPage'
import { SettingsPage } from '@/routes/settings/SettingsPage'
import { TranslationReviewPage } from '@/routes/review/TranslationReviewPage'
import { VoiceLibraryPage } from '@/routes/voices/VoiceLibraryPage'
import { VoiceReviewPage } from '@/routes/review/VoiceReviewPage'
import { resolveAppBasePath } from '@/lib/app/config'

export const router = createBrowserRouter(
  [
    {
      path: '/',
      element: <AppShell />,
      children: [
        {
          index: true,
          element: <Navigate replace to="/translations/new" />,
        },
        {
          path: 'translations/new',
          element: <NewTranslationPage />,
        },
        {
          path: 'tasks/current',
          element: <CurrentTaskPage />,
        },
        {
          path: 'projects',
          element: <MyProjectsPage />,
        },
        {
          path: 'projects/:jobId',
          element: <ProjectDetailPage />,
        },
        {
          path: 'voices',
          element: <VoiceLibraryPage />,
        },
        {
          path: 'settings',
          element: <SettingsPage />,
        },
        {
          path: 'reviews/:jobId/speaker',
          element: <SpeakerReviewPage />,
        },
        {
          path: 'reviews/:jobId/translation',
          element: <TranslationReviewPage />,
        },
        {
          path: 'reviews/:jobId/voice',
          element: <VoiceReviewPage />,
        },
      ],
    },
    {
      path: '*',
      element: <NotFoundPage />,
    },
  ],
  {
    basename: resolveAppBasePath(),
  },
)
