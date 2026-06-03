import { useEffect, useRef, useState } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import {
  fetchUserAttributes,
  fetchAuthSession,
  getCurrentUser,
  signOut,
} from 'aws-amplify/auth'
import './HomePage.css'
import { useLanguageControl } from '../language-control/LanguageControlProvider.jsx'
import { deleteCourse, getCourseProgress, getUserCourses } from '../services/coursesService.js'
import { getCourseDocuments } from '../services/documentsService.js'
import { buildCourseCardStats } from '../utils/courseCardStats.js'

function IconHome() {
  return (
    <svg className="home-page__nav-icon" width="20" height="20" viewBox="0 0 24 24" aria-hidden>
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M3 10.25 12 3l9 7.25V20a.75.75 0 01-.75.75h-4.5v-6h-3v6H3.75A.75.75 0 013 20v-9.75z"
      />
    </svg>
  )
}

function IconLogout() {
  return (
    <svg className="home-page__nav-icon" width="20" height="20" viewBox="0 0 24 24" aria-hidden>
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M15 12H4m11 0l-3-3m3 3l-3 3M8 5V4a1 1 0 011-1h9a1 1 0 011 1v16a1 1 0 01-1 1H9a1 1 0 01-1-1v-1"
      />
    </svg>
  )
}

function IconNavDocuments() {
  return (
    <svg className="home-page__nav-icon" width="20" height="20" viewBox="0 0 24 24" aria-hidden>
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M14.25 2.25h-4.5L7.5 4.5H3.75A1.5 1.5 0 002.25 6v13.5A1.5 1.5 0 003.75 21h16.5a1.5 1.5 0 001.5-1.5V6a1.5 1.5 0 00-1.5-1.5h-3.75L14.25 2.25z"
      />
    </svg>
  )
}

function IconNavAnalytics() {
  return (
    <svg className="home-page__nav-icon" width="20" height="20" viewBox="0 0 24 24" aria-hidden>
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M3 3v18h18M7 16l4-4 4 3 5-6"
      />
    </svg>
  )
}

function IconNavSettings() {
  return (
    <svg className="home-page__nav-icon" width="20" height="20" viewBox="0 0 24 24" aria-hidden>
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 15a3 3 0 100-6 3 3 0 000 6z M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.6a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9c.26.604.852 1 1.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"
      />
    </svg>
  )
}

function IconSearch() {
  return (
    <svg className="home-page__toolbar-icon" width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        d="M11 19a8 8 0 100-16 8 8 0 000 16zm10 2-4.35-4.35"
      />
    </svg>
  )
}

function IconBell() {
  return (
    <svg className="home-page__toolbar-icon" width="20" height="20" viewBox="0 0 24 24" aria-hidden>
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11c0-3.866-3.134-7-7-7S4 7.134 4 11v3.159c0 .538-.214 1.055-.595 1.436L2 17h5m8 0a3 3 0 11-6 0h6z"
      />
    </svg>
  )
}

function IconFilter() {
  return (
    <svg className="home-page__filter-icon" width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        d="M4 6h16M8 12h8M10 18h4"
      />
      <circle cx="18" cy="6" r="1.5" fill="currentColor" />
      <circle cx="6" cy="12" r="1.5" fill="currentColor" />
      <circle cx="14" cy="18" r="1.5" fill="currentColor" />
    </svg>
  )
}

function IconMetaDoc() {
  return (
    <svg className="home-page__meta-icon" width="16" height="16" viewBox="0 0 24 24" aria-hidden>
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M14.25 2.25h-6a1.5 1.5 0 00-1.5 1.5v16.5a1.5 1.5 0 001.5 1.5h10.5a1.5 1.5 0 001.5-1.5V7.5L14.25 2.25z M14.25 2.25V7.5h5.25"
      />
    </svg>
  )
}

function IconMetaClock() {
  return (
    <svg className="home-page__meta-icon" width="16" height="16" viewBox="0 0 24 24" aria-hidden>
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" d="M12 7v5l3 2" />
    </svg>
  )
}

function IconChevronEnd() {
  return (
    <svg
      className="home-page__course-chevron"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      aria-hidden
    >
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9 6l6 6-6 6"
      />
    </svg>
  )
}

function IconPlus() {
  return (
    <svg
      className="home-page__course-icon home-page__course-icon--plus"
      width="22"
      height="22"
      viewBox="0 0 24 24"
      aria-hidden
    >
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        d="M12 5v14M5 12h14"
      />
    </svg>
  )
}

function IconTrash() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden>
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9 3.75h6m-7.5 3h9m-7.5 3.75v7.5m3-7.5v7.5m4.875-10.5-.662 9.272A2.25 2.25 0 0 1 13.97 21h-3.94a2.25 2.25 0 0 1-2.243-2.028L7.125 7.5"
      />
    </svg>
  )
}

function logAuthError(context, error) {
  const message = error?.message ?? String(error)
  const name = error?.name ?? error?.code
  console.warn('[Auth i18n draft]', context, { name, message, error })
}

function normalizeCourseId(course) {
  const raw = course?.course_id ?? course?.id ?? course?.courseId
  return String(raw ?? '').trim()
}

function activeCourseIdSet(courses) {
  return new Set(
    (Array.isArray(courses) ? courses : [])
      .map(normalizeCourseId)
      .filter(Boolean),
  )
}

function pruneCourseStatsToActive(prev, activeIds) {
  if (!prev || typeof prev !== 'object') return {}
  return Object.fromEntries(
    Object.entries(prev).filter(([id]) => activeIds.has(id)),
  )
}

function formatRelativeActivity(iso, lang) {
  if (!iso || typeof iso !== 'string') return null
  const then = new Date(iso)
  if (Number.isNaN(then.getTime())) return null
  const diffSec = (then.getTime() - Date.now()) / 1000
  const abs = Math.abs(diffSec)
  const rtf = new Intl.RelativeTimeFormat(lang === 'he' ? 'he' : 'en', { numeric: 'auto' })
  if (abs < 60) return rtf.format(Math.round(diffSec), 'second')
  if (abs < 3600) return rtf.format(Math.round(diffSec / 60), 'minute')
  if (abs < 86400) return rtf.format(Math.round(diffSec / 3600), 'hour')
  if (abs < 86400 * 7) return rtf.format(Math.round(diffSec / 86400), 'day')
  if (abs < 86400 * 30) return rtf.format(Math.round(diffSec / (86400 * 7)), 'week')
  if (abs < 86400 * 365) return rtf.format(Math.round(diffSec / (86400 * 30)), 'month')
  return rtf.format(Math.round(diffSec / (86400 * 365)), 'year')
}

function pickDisplayInitials(name) {
  const s = String(name || '').trim()
  if (!s) return '?'
  const parts = s.split(/\s+/).filter(Boolean)
  if (parts.length >= 2) {
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
  }
  return s.slice(0, 2).toUpperCase()
}

export default function HomePage() {
  const navigate = useNavigate()
  const location = useLocation()
  const { t, lang, setLang, dir, tx } = useLanguageControl()
  const [status, setStatus] = useState('loading')
  const [displayName, setDisplayName] = useState('')
  const [isCreateCourseOpen, setIsCreateCourseOpen] = useState(false)
  const [isCreatingCourse, setIsCreatingCourse] = useState(false)
  const [createCourseError, setCreateCourseError] = useState('')
  const [currentUserId, setCurrentUserId] = useState('')
  const [courses, setCourses] = useState([])
  const [isCoursesLoading, setIsCoursesLoading] = useState(false)
  const [coursesError, setCoursesError] = useState('')
  const [coursesRefreshKey, setCoursesRefreshKey] = useState(0)
  const [isDeleteCourseOpen, setIsDeleteCourseOpen] = useState(false)
  const [isDeletingCourse, setIsDeletingCourse] = useState(false)
  const [deleteCourseError, setDeleteCourseError] = useState('')
  const [courseToDelete, setCourseToDelete] = useState(null)
  const [courseStatsById, setCourseStatsById] = useState({})
  const [courseStatsLoading, setCourseStatsLoading] = useState(false)
  const statsFetchGenerationRef = useRef(0)
  const apiBaseUrl = import.meta.env.VITE_API_URL ?? ''
  const [courseDraft, setCourseDraft] = useState({
    name: '',
    description: '',
    visibility: 'private',
  })

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const user = await getCurrentUser()
        let attrs = {}
        try {
          attrs = await fetchUserAttributes()
        } catch (attrErr) {
          logAuthError('fetchUserAttributes', attrErr)
        }
        if (cancelled) return
        const name =
          attrs.given_name ??
          attrs.name ??
          attrs.preferred_username ??
          user.username
        setCurrentUserId(String(user.userId ?? attrs.sub ?? '').trim())
        setDisplayName(String(name || user.username || '').trim() || 'Guest')
        setStatus('authed')
      } catch (e) {
        logAuthError('getCurrentUser / fetchUserAttributes', e)
        if (!cancelled) setStatus('guest')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      if (status !== 'authed' || !currentUserId) return

      try {
        setIsCoursesLoading(true)
        setCoursesError('')

        const session = await fetchAuthSession()
        const idToken = session.tokens?.idToken?.toString()
        if (!idToken) {
          throw new Error('Missing authentication token.')
        }

        const items = await getUserCourses(currentUserId, idToken)
        if (!cancelled) {
          setCourses(Array.isArray(items) ? items : [])
        }
      } catch (error) {
        console.error('[get-my-courses-failed]', error)
        if (!cancelled) {
          setCourses([])
          setCoursesError(error?.message || 'Could not load your courses.')
        }
      } finally {
        if (!cancelled) setIsCoursesLoading(false)
      }
    })()

    return () => {
      cancelled = true
    }
  }, [currentUserId, status, coursesRefreshKey])

  // Per-course materials + progress (2×N GETs). Future: summary endpoint on list API.
  useEffect(() => {
    let cancelled = false
    const generation = ++statsFetchGenerationRef.current

    ;(async () => {
      if (status !== 'authed' || !currentUserId) return

      const activeIds = activeCourseIdSet(courses)
      setCourseStatsById((prev) => pruneCourseStatsToActive(prev, activeIds))

      if (!courses.length) {
        if (!cancelled && generation === statsFetchGenerationRef.current) {
          setCourseStatsById({})
          setCourseStatsLoading(false)
        }
        return
      }

      const coursesSnapshot = [...courses]
      const snapshotIds = activeCourseIdSet(coursesSnapshot)

      try {
        setCourseStatsLoading(true)

        const session = await fetchAuthSession()
        const idToken = session.tokens?.idToken?.toString()
        if (!idToken) {
          throw new Error('Missing authentication token.')
        }

        const coursePromises = coursesSnapshot.map(async (course) => {
          const courseId = normalizeCourseId(course)
          if (!courseId) return { courseId: '', stats: null }

          const [docsResult, progressResult] = await Promise.allSettled([
            getCourseDocuments(courseId, idToken),
            getCourseProgress(courseId, idToken),
          ])

          const documents =
            docsResult.status === 'fulfilled' && Array.isArray(docsResult.value)
              ? docsResult.value
              : []
          const progressPayload =
            progressResult.status === 'fulfilled' ? progressResult.value : null

          return {
            courseId,
            stats: buildCourseCardStats(course, documents, progressPayload),
          }
        })

        const results = await Promise.allSettled(coursePromises)
        if (cancelled || generation !== statsFetchGenerationRef.current) return

        const merged = {}
        for (const result of results) {
          if (result.status !== 'fulfilled') continue
          const { courseId, stats } = result.value
          if (!courseId || !stats || !snapshotIds.has(courseId)) continue
          merged[courseId] = stats
        }

        setCourseStatsById((prev) => {
          const prunedPrev = pruneCourseStatsToActive(prev, snapshotIds)
          return { ...prunedPrev, ...merged }
        })
      } catch (error) {
        console.error('[home-course-stats-failed]', error)
        if (!cancelled && generation === statsFetchGenerationRef.current) {
          setCourseStatsById((prev) => pruneCourseStatsToActive(prev, snapshotIds))
        }
      } finally {
        if (!cancelled && generation === statsFetchGenerationRef.current) {
          setCourseStatsLoading(false)
        }
      }
    })()

    return () => {
      cancelled = true
    }
  }, [
    status,
    currentUserId,
    courses,
    coursesRefreshKey,
    location.pathname,
    location.key,
  ])

  const handleLogout = async () => {
    try {
      await signOut()
    } catch (e) {
      logAuthError('signOut', e)
    }
    navigate('/', { replace: true })
  }

  const updateCourseDraft = (field, value) => {
    setCourseDraft((prev) => ({ ...prev, [field]: value }))
  }

  const closeCreateCourseModal = () => {
    setIsCreateCourseOpen(false)
    setCreateCourseError('')
    setCourseDraft({
      name: '',
      description: '',
      visibility: 'private',
    })
  }

  const closeDeleteCourseModal = () => {
    if (isDeletingCourse) return
    setIsDeleteCourseOpen(false)
    setDeleteCourseError('')
    setCourseToDelete(null)
  }

  const handleDeleteCourseClick = (e, course) => {
    e.stopPropagation()
    const courseId = String(course?.course_id ?? course?.id ?? course?.courseId ?? '').trim()
    const courseName = String(course?.course_name ?? course?.name ?? t.home.untitledCourse)
    if (!courseId) return
    setCourseToDelete({ id: courseId, name: courseName })
    setDeleteCourseError('')
    setIsDeleteCourseOpen(true)
  }

  const handleConfirmDeleteCourse = async () => {
    if (!courseToDelete?.id || isDeletingCourse) return
    setDeleteCourseError('')
    try {
      setIsDeletingCourse(true)
      const session = await fetchAuthSession()
      const idToken = session.tokens?.idToken?.toString()
      if (!idToken) {
        throw new Error(t.home.deleteCourseMissingSession)
      }

      await deleteCourse(courseToDelete.id, idToken)
      const deletedId = courseToDelete.id
      setCourses((prev) => {
        const next = prev.filter((course, index) => {
          const id = String(course.course_id ?? course.id ?? course.courseId ?? `course-${index}`)
          return id !== deletedId
        })
        return next
      })
      setCourseStatsById((prev) => {
        if (!prev[deletedId]) return prev
        const next = { ...prev }
        delete next[deletedId]
        return next
      })
      setIsDeleteCourseOpen(false)
      setCourseToDelete(null)
      setDeleteCourseError('')
    } catch (error) {
      const apiMessage = error?.response?.data?.message
      if (typeof apiMessage === 'string' && apiMessage.trim()) {
        setDeleteCourseError(apiMessage.trim())
      } else {
        setDeleteCourseError(error?.message || t.home.deleteCourseError)
      }
    } finally {
      setIsDeletingCourse(false)
    }
  }

  const handleCreateCourseSubmit = async (e) => {
    e.preventDefault()
    setCreateCourseError('')

    if (!apiBaseUrl) {
      setCreateCourseError('API is not configured. Set VITE_API_URL.')
      return
    }

    try {
      setIsCreatingCourse(true)
      const session = await fetchAuthSession()
      const accessToken = session.tokens?.idToken?.toString()

      if (!accessToken) {
        setCreateCourseError('You are not authenticated. Please sign in again.')
        return
      }

      const response = await fetch(`${apiBaseUrl}/courses`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${accessToken}`,
        },
        body: JSON.stringify({
          course_name: courseDraft.name.trim(),
          description: courseDraft.description.trim(),
          is_public: courseDraft.visibility === 'public',
        }),
      })

      const payload = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(payload?.message || `Failed with status ${response.status}`)
      }

      closeCreateCourseModal()
      setCoursesRefreshKey((prev) => prev + 1)
    } catch (error) {
      console.error('[create-course-failed]', error)
      setCreateCourseError(error?.message || 'Could not create course.')
    } finally {
      setIsCreatingCourse(false)
    }
  }

  if (status === 'loading') {
    return (
      <main className="home-page" dir={dir} lang={lang}>
        <section className="home-page__loading">
          <p className="home-page__greeting home-page__greeting--muted">{t.home.loading}</p>
        </section>
      </main>
    )
  }

  if (status === 'guest') {
    return <Navigate to="/" replace />
  }

  return (
    <main className="home-page" dir={dir} lang={lang}>
      <aside className="home-page__sidebar" aria-label={t.home.navLabel}>
        <div className="home-page__brand">
          <div className="home-page__logo" aria-hidden />
          <p className="home-page__brand-name">{t.home.brandName}</p>
        </div>

        <nav className="home-page__menu" aria-label={t.home.navLabel}>
          <button type="button" className="home-page__menu-item home-page__menu-item--active">
            <IconHome />
            <span>{t.home.dashboard}</span>
          </button>
          <button type="button" className="home-page__menu-item">
            <IconNavDocuments />
            <span>{t.home.documents}</span>
          </button>
          <button type="button" className="home-page__menu-item">
            <IconNavAnalytics />
            <span>{t.home.analytics}</span>
          </button>
        </nav>

        <div className="home-page__sidebar-spacer" aria-hidden />

        <div className="home-page__sidebar-footer">
          <div className="home-page__lang-switch" role="group" aria-label={t.common.switchLanguage}>
            <button
              type="button"
              className={`home-page__lang-btn ${lang === 'he' ? 'home-page__lang-btn--active' : ''}`}
              onClick={() => setLang('he')}
            >
              {t.common.langHe}
            </button>
            <button
              type="button"
              className={`home-page__lang-btn ${lang === 'en' ? 'home-page__lang-btn--active' : ''}`}
              onClick={() => setLang('en')}
            >
              {t.common.langEn}
            </button>
          </div>
          <nav className="home-page__menu home-page__menu--compact" aria-label={t.home.settings}>
            <button type="button" className="home-page__menu-item">
              <IconNavSettings />
              <span>{t.home.settings}</span>
            </button>
          </nav>
        </div>

        <button type="button" className="home-page__logout" onClick={handleLogout}>
          <IconLogout />
          <span>{t.home.logout}</span>
        </button>
      </aside>

      <section className="home-page__content">
        <header className="home-page__top-bar">
          <div className="home-page__top-bar-welcome">
            <h1 className="home-page__greeting">
              {tx(t.home.greeting, { name: displayName || 'Guest' })}
            </h1>
            <p className="home-page__subtext">{t.home.subtext}</p>
          </div>
          <div className="home-page__top-bar-tools">
            <label className="home-page__search">
              <span className="home-page__search-icon">
                <IconSearch />
              </span>
              <input
                type="search"
                className="home-page__search-input"
                placeholder={t.home.searchCoursesPlaceholder}
                readOnly
                tabIndex={0}
                aria-label={t.home.searchCoursesPlaceholder}
              />
            </label>
            <button type="button" className="home-page__icon-btn" aria-label={t.home.notificationsAria}>
              <IconBell />
            </button>
            <div className="home-page__avatar" role="img" aria-label={t.home.profilePhotoAria}>
              {pickDisplayInitials(displayName)}
            </div>
          </div>
        </header>

        <section className="home-page__courses-section" aria-live="polite">
          <header className="home-page__courses-header">
            <div className="home-page__courses-header-text">
              <h2 className="home-page__courses-title">{t.home.myCourses}</h2>
              <p className="home-page__courses-subtitle">{t.home.myCoursesSubtitle}</p>
            </div>
            <button type="button" className="home-page__filter-btn">
              <IconFilter />
              <span>{t.home.filterCourses}</span>
            </button>
          </header>

          {isCoursesLoading ? (
            <p className="home-page__courses-state">{t.home.coursesLoading}</p>
          ) : null}

          {!isCoursesLoading && coursesError ? (
            <p className="home-page__courses-error" role="alert">
              {t.home.coursesError}
            </p>
          ) : null}

          {!isCoursesLoading && !coursesError ? (
            <ul className="home-page__courses-grid">
              {courses.length === 0 ? (
                <li className="home-page__courses-grid-item home-page__courses-grid-item--empty">
                  <p className="home-page__courses-state">{t.home.coursesEmpty}</p>
                </li>
              ) : null}
              {courses.map((course) => {
                const courseId = normalizeCourseId(course)
                const courseName =
                  course.course_name ?? course.name ?? t.home.untitledCourse
                const stats = courseId ? courseStatsById[courseId] : null
                const statsPending = courseStatsLoading && !stats
                const docCount = stats?.documentCount ?? 0
                const progressPct = stats?.progressPercent ?? 0
                const lastUpdatedIso =
                  stats?.lastUpdatedIso ??
                  (typeof (course.created_at ?? course.createdAt) === 'string'
                    ? course.created_at ?? course.createdAt
                    : null)
                const activityPhrase = formatRelativeActivity(lastUpdatedIso, lang)
                const activityTime = activityPhrase ?? t.home.courseMetaUnknown
                const activityLabel = tx(t.home.courseMetaActivity, { time: activityTime })
                const docsLabel = statsPending
                  ? t.home.courseMetaDocsLoading
                  : tx(t.home.courseMetaDocs, { count: docCount })
                return (
                  <li key={String(courseId || courseName)} className="home-page__courses-grid-item">
                    <div className="home-page__course-card-shell">
                      <button
                        type="button"
                        className="home-page__course-card"
                        onClick={() => {
                          if (!courseId) return
                          navigate(`/course/${encodeURIComponent(String(courseId))}`, {
                            state: { courseName },
                          })
                        }}
                      >
                        <div className="home-page__course-card-body">
                          <span className="home-page__course-name">{courseName}</span>
                          <div
                            className="home-page__course-meta"
                            aria-label={
                              statsPending ? t.home.courseCardMetaLoadingAria : undefined
                            }
                          >
                            <span
                              className={`home-page__course-meta-item${statsPending ? ' home-page__course-meta-item--pending' : ''}`}
                            >
                              <IconMetaDoc />
                              <span>{docsLabel}</span>
                            </span>
                            <span
                              className={`home-page__course-meta-item${statsPending ? ' home-page__course-meta-item--pending' : ''}`}
                            >
                              <IconMetaClock />
                              <span>{activityLabel}</span>
                            </span>
                          </div>
                          <div
                            className="home-page__course-progress-block"
                            aria-busy={statsPending ? 'true' : undefined}
                          >
                            <div className="home-page__course-progress-labels">
                              <span>{t.home.progressLabel}</span>
                              <span>{progressPct}%</span>
                            </div>
                            <div
                              className="home-page__course-progress-track"
                              role="progressbar"
                              aria-valuenow={progressPct}
                              aria-valuemin={0}
                              aria-valuemax={100}
                              aria-label={t.home.progressLabel}
                            >
                              <span
                                className="home-page__course-progress-fill"
                                style={{ width: `${progressPct}%` }}
                              />
                            </div>
                          </div>
                        </div>
                        <IconChevronEnd />
                      </button>
                      <button
                        type="button"
                        className="home-page__course-delete-btn"
                        onClick={(e) => handleDeleteCourseClick(e, course)}
                        disabled={isDeletingCourse}
                        aria-label={tx(t.home.deleteCourseAria, { name: courseName })}
                      >
                        <IconTrash />
                      </button>
                    </div>
                  </li>
                )
              })}
              <li className="home-page__courses-grid-item">
                <button
                  type="button"
                  className="home-page__course-card home-page__course-card--create"
                  onClick={() => setIsCreateCourseOpen(true)}
                >
                  <span className="home-page__create-fab" aria-hidden>
                    <IconPlus />
                  </span>
                  <span className="home-page__course-name home-page__course-name--create">{t.home.createCourse}</span>
                  <span className="home-page__create-hint">{t.home.createCourseCardHint}</span>
                </button>
              </li>
            </ul>
          ) : null}
        </section>
      </section>

      {isCreateCourseOpen ? (
        <div
          className="home-page__modal-backdrop"
          role="presentation"
          onClick={closeCreateCourseModal}
        >
          <section
            className="home-page__modal"
            role="dialog"
            aria-modal="true"
            aria-label={t.home.createCourseModalTitle}
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="home-page__modal-title">{t.home.createCourseModalTitle}</h2>
            <p className="home-page__modal-subtitle">{t.home.createCourseModalSubtitle}</p>
            <form onSubmit={handleCreateCourseSubmit} className="home-page__modal-form">
              <label className="home-page__modal-label" htmlFor="course-name">
                {t.home.courseNameLabel}
              </label>
              <input
                id="course-name"
                className="home-page__modal-input"
                type="text"
                value={courseDraft.name}
                onChange={(e) => updateCourseDraft('name', e.target.value)}
                placeholder={t.home.courseNamePlaceholder}
              />

              <label className="home-page__modal-label" htmlFor="course-description">
                {t.home.courseDescriptionLabel}
              </label>
              <textarea
                id="course-description"
                className="home-page__modal-textarea"
                rows={4}
                value={courseDraft.description}
                onChange={(e) => updateCourseDraft('description', e.target.value)}
                placeholder={t.home.courseDescriptionPlaceholder}
              />

              <fieldset className="home-page__visibility-group">
                <legend className="home-page__modal-label">{t.home.visibilityLabel}</legend>
                <label className="home-page__radio-option">
                  <input
                    type="radio"
                    name="visibility"
                    value="private"
                    checked={courseDraft.visibility === 'private'}
                    onChange={(e) => updateCourseDraft('visibility', e.target.value)}
                  />
                  <span>{t.home.visibilityPrivate}</span>
                </label>
                <label className="home-page__radio-option">
                  <input
                    type="radio"
                    name="visibility"
                    value="public"
                    checked={courseDraft.visibility === 'public'}
                    onChange={(e) => updateCourseDraft('visibility', e.target.value)}
                  />
                  <span>{t.home.visibilityPublic}</span>
                </label>
              </fieldset>

              <div className="home-page__modal-actions">
                <button
                  type="button"
                  className="home-page__modal-cancel"
                  disabled={isCreatingCourse}
                  onClick={closeCreateCourseModal}
                >
                  {t.home.cancel}
                </button>
                <button type="submit" className="home-page__modal-submit" disabled={isCreatingCourse}>
                  {isCreatingCourse ? t.home.creatingCourse : t.home.saveCourse}
                </button>
              </div>
              {createCourseError ? (
                <p className="home-page__modal-error" role="alert">
                  {createCourseError}
                </p>
              ) : null}
            </form>
          </section>
        </div>
      ) : null}
      {isDeleteCourseOpen ? (
        <div
          className="home-page__modal-backdrop"
          role="presentation"
          onClick={closeDeleteCourseModal}
        >
          <section
            className="home-page__modal"
            role="dialog"
            aria-modal="true"
            aria-label={t.home.deleteCourseModalTitle}
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="home-page__modal-title">{t.home.deleteCourseModalTitle}</h2>
            <p className="home-page__modal-subtitle">
              {tx(t.home.deleteCoursePrompt, { name: courseToDelete?.name || t.home.untitledCourse })}
            </p>
            <div className="home-page__modal-actions">
              <button
                type="button"
                className="home-page__modal-cancel"
                disabled={isDeletingCourse}
                onClick={closeDeleteCourseModal}
              >
                {t.home.cancel}
              </button>
              <button
                type="button"
                className="home-page__modal-submit home-page__modal-submit--danger"
                disabled={isDeletingCourse}
                onClick={handleConfirmDeleteCourse}
              >
                {isDeletingCourse ? t.home.deleteCourseDeleting : t.home.deleteCourseConfirm}
              </button>
            </div>
            {deleteCourseError ? (
              <p className="home-page__modal-error" role="alert">
                {deleteCourseError}
              </p>
            ) : null}
          </section>
        </div>
      ) : null}
    </main>
  )
}
