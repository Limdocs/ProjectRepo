import { useCallback, useEffect, useRef, useState } from 'react'
import { Navigate, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { fetchAuthSession, getCurrentUser } from 'aws-amplify/auth'
import './CoursePage.css'
import { useLanguageControl } from '../language-control/LanguageControlProvider.jsx'
import { deleteCourse, getUserCourses } from '../services/coursesService.js'
import {
  deleteDocument,
  deleteQuestionSet,
  generateQuiz,
  getCourseDocuments,
  getQuestionSetDetails,
  getQuestionSets,
  MAX_UPLOAD_BYTES,
  getUploadUrl,
  uploadFileToS3,
} from '../services/documentsService.js'

const FINAL_PROCESSING_STATUSES = new Set(['READY', 'FAILED', 'ERROR'])
const QUIZ_ELIGIBLE_STATUSES = new Set(['READY'])

function fileKey(file) {
  return `${file.name}-${file.size}-${file.lastModified}`
}

function mergeFileLists(prev, incoming) {
  const map = new Map()
  for (const f of prev) map.set(fileKey(f), f)
  for (const f of incoming) map.set(fileKey(f), f)
  return Array.from(map.values())
}

function formatFileSize(bytes) {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${parseFloat((bytes / k ** i).toFixed(i > 0 ? 1 : 0))} ${sizes[i]}`
}

function formatDocumentDate(iso, lang) {
  if (!iso || typeof iso !== 'string') return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString(lang === 'he' ? 'he-IL' : 'en-US', {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function normalizeProcessingStatus(status) {
  return String(status ?? '').trim().toUpperCase()
}

export default function CoursePage() {
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const { courseId: courseIdParam } = useParams()
  const { t, lang, setLang, dir, tx } = useLanguageControl()
  const [authStatus, setAuthStatus] = useState('loading')
  const [activeTab, setActiveTab] = useState(() =>
    searchParams.get('tab') === 'questionSets' ? 'questionSets' : 'materials',
  )
  const [documents, setDocuments] = useState([])
  const [documentsLoading, setDocumentsLoading] = useState(false)
  const [documentsError, setDocumentsError] = useState(null)
  const [documentsNotice, setDocumentsNotice] = useState(null)
  const [deletingDocId, setDeletingDocId] = useState(null)
  const [isDeleteModalOpen, setIsDeleteModalOpen] = useState(false)
  const [pendingDeleteDoc, setPendingDeleteDoc] = useState(null)
  const [isDeleteCourseModalOpen, setIsDeleteCourseModalOpen] = useState(false)
  const [isDeletingCourse, setIsDeletingCourse] = useState(false)
  const [deleteCourseError, setDeleteCourseError] = useState(null)
  const [isUploadModalOpen, setIsUploadModalOpen] = useState(false)
  const [selectedFiles, setSelectedFiles] = useState([])
  const [isDraggingOverDropzone, setIsDraggingOverDropzone] = useState(false)
  const [isUploading, setIsUploading] = useState(false)
  const [uploadProgress, setUploadProgress] = useState(null)
  const [uploadError, setUploadError] = useState(null)
  const [uploadSuccess, setUploadSuccess] = useState(false)
  const [selectionMode, setSelectionMode] = useState(false)
  const [selectedDocIds, setSelectedDocIds] = useState([])
  const [isGeneratingQuiz, setIsGeneratingQuiz] = useState(false)
  const [quizError, setQuizError] = useState(null)
  const [quizStartedNotice, setQuizStartedNotice] = useState(null)
  const [quizOverlayMessageIndex, setQuizOverlayMessageIndex] = useState(0)
  const [questionSets, setQuestionSets] = useState([])
  const [questionSetsLoading, setQuestionSetsLoading] = useState(false)
  const [questionSetsError, setQuestionSetsError] = useState(null)
  const [selectedQuestionSet, setSelectedQuestionSet] = useState(() => {
    const setId = searchParams.get('set')
    return setId ? { set_id: setId } : null
  })
  const [setQuestions, setSetQuestions] = useState([])
  const [setQuestionsLoading, setSetQuestionsLoading] = useState(false)
  const [setQuestionsError, setSetQuestionsError] = useState(null)
  const [questionMode, setQuestionMode] = useState('review')
  const [practiceAnswers, setPracticeAnswers] = useState({})
  const [setPendingDelete, setSetPendingDelete] = useState(null)
  const [isDeletingSet, setIsDeletingSet] = useState(false)
  const [questionSetsNotice, setQuestionSetsNotice] = useState(null)
  const fileInputRef = useRef(null)
  const dragDepthRef = useRef(0)

  const initialCourseName =
    typeof location.state?.courseName === 'string' ? location.state.courseName.trim() : ''
  const [courseName, setCourseName] = useState(initialCourseName)
  const displayCourseName = courseName || t.home.untitledCourse
  const courseId = courseIdParam?.trim() ?? ''

  useEffect(() => {
    if (!initialCourseName || initialCourseName === courseName) return
    setCourseName(initialCourseName)
  }, [initialCourseName, courseName])

  useEffect(() => {
    if (!courseId || courseName) return
    let cancelled = false

    ;(async () => {
      try {
        const user = await getCurrentUser()
        const userId = String(user?.userId ?? '').trim()
        if (!userId) return
        const session = await fetchAuthSession()
        const idToken = session.tokens?.idToken?.toString()
        if (!idToken) return
        const courses = await getUserCourses(userId, idToken)
        if (cancelled) return
        const matchedCourse = Array.isArray(courses)
          ? courses.find((course) => {
              const id = String(course?.course_id ?? course?.id ?? course?.courseId ?? '').trim()
              return id === courseId
            })
          : null
        const fallbackName = String(
          matchedCourse?.course_name ?? matchedCourse?.name ?? '',
        ).trim()
        if (fallbackName) {
          setCourseName(fallbackName)
        }
      } catch {
        // Keep UI fallback title when metadata fetch fails.
      }
    })()

    return () => {
      cancelled = true
    }
  }, [courseId, courseName])

  const loadDocuments = useCallback(async () => {
    if (!courseId) return
    setDocumentsLoading(true)
    setDocumentsError(null)
    setDocumentsNotice(null)
    try {
      const session = await fetchAuthSession()
      const idToken = session.tokens?.idToken?.toString()
      if (!idToken) {
        setDocuments([])
        setDocumentsError(t.coursePage.uploadMissingSession)
        return
      }
      const list = await getCourseDocuments(courseId, idToken)
      setDocuments(list)
    } catch (err) {
      let message = t.coursePage.documentsError
      const apiMsg = err?.response?.data?.message
      if (typeof apiMsg === 'string' && apiMsg.trim()) {
        message = apiMsg.trim()
      } else if (typeof err?.message === 'string' && err.message.includes('VITE_API_URL')) {
        message = t.coursePage.uploadApiNotConfigured
      }
      setDocumentsError(message)
      setDocuments([])
    } finally {
      setDocumentsLoading(false)
    }
  }, [courseId, t])

  const loadQuestionSets = useCallback(async () => {
    if (!courseId) return
    setQuestionSetsLoading(true)
    setQuestionSetsError(null)
    try {
      const session = await fetchAuthSession()
      const idToken = session.tokens?.idToken?.toString()
      if (!idToken) {
        setQuestionSets([])
        setQuestionSetsError(t.coursePage.uploadMissingSession)
        return
      }
      const sets = await getQuestionSets(courseId, idToken)
      setQuestionSets(sets)
    } catch (err) {
      const apiMsg = err?.response?.data?.message
      setQuestionSetsError(
        typeof apiMsg === 'string' && apiMsg.trim()
          ? apiMsg.trim()
          : t.coursePage.questionSetsLoadError,
      )
      setQuestionSets([])
    } finally {
      setQuestionSetsLoading(false)
    }
  }, [courseId, t])

  const loadQuestionSetDetails = useCallback(
    async (setId) => {
      if (!courseId || !setId) return
      setSetQuestionsLoading(true)
      setSetQuestionsError(null)
      try {
        const session = await fetchAuthSession()
        const idToken = session.tokens?.idToken?.toString()
        if (!idToken) {
          setSetQuestions([])
          setSetQuestionsError(t.coursePage.uploadMissingSession)
          return
        }
        const payload = await getQuestionSetDetails(courseId, setId, idToken)
        setSelectedQuestionSet(payload?.set ?? null)
        setSetQuestions(Array.isArray(payload?.questions) ? payload.questions : [])
        setPracticeAnswers({})
      } catch (err) {
        const apiMsg = err?.response?.data?.message
        setSetQuestionsError(
          typeof apiMsg === 'string' && apiMsg.trim()
            ? apiMsg.trim()
            : t.coursePage.questionSetLoadError,
        )
      } finally {
        setSetQuestionsLoading(false)
      }
    },
    [courseId, t],
  )

  useEffect(() => {
    if (authStatus !== 'authed' || !courseId || activeTab !== 'materials') return
    loadDocuments()
  }, [authStatus, courseId, activeTab, loadDocuments])

  useEffect(() => {
    if (authStatus !== 'authed' || !courseId || activeTab !== 'questionSets') return
    loadQuestionSets()
  }, [authStatus, courseId, activeTab, loadQuestionSets])

  useEffect(() => {
    if (authStatus !== 'authed' || activeTab !== 'questionSets' || !selectedQuestionSet?.set_id) return
    loadQuestionSetDetails(selectedQuestionSet.set_id)
  }, [authStatus, activeTab, selectedQuestionSet?.set_id, loadQuestionSetDetails])

  const eligibleDocIds = documents.reduce((acc, doc, index) => {
    const id = String(doc.document_id ?? doc.documentId ?? `doc-${index}`)
    const status = normalizeProcessingStatus(doc.processing_status ?? doc.processingStatus)
    if (QUIZ_ELIGIBLE_STATUSES.has(status)) {
      acc.push(id)
    }
    return acc
  }, [])

  useEffect(() => {
    if (selectedDocIds.length === 0) return
    const eligibleSet = new Set(eligibleDocIds)
    setSelectedDocIds((prev) => {
      const next = prev.filter((id) => eligibleSet.has(id))
      if (next.length === prev.length && next.every((id, index) => id === prev[index])) {
        return prev
      }
      return next
    })
  }, [eligibleDocIds, selectedDocIds.length])

  useEffect(() => {
    if (authStatus !== 'authed' || !courseId) return undefined

    const hasNonFinalDocuments = documents.some((doc) => {
      const status = normalizeProcessingStatus(doc.processing_status ?? doc.processingStatus)
      return !FINAL_PROCESSING_STATUSES.has(status)
    })

    if (!hasNonFinalDocuments) return undefined

    const intervalId = window.setInterval(() => {
      loadDocuments()
    }, 7000)

    return () => window.clearInterval(intervalId)
  }, [authStatus, courseId, documents, loadDocuments])

  const closeUploadModal = useCallback(() => {
    dragDepthRef.current = 0
    setIsDraggingOverDropzone(false)
    setSelectedFiles([])
    setIsUploadModalOpen(false)
    setIsUploading(false)
    setUploadProgress(null)
    setUploadError(null)
    setUploadSuccess(false)
  }, [])

  const closeDeleteModal = useCallback(() => {
    if (deletingDocId) return
    setIsDeleteModalOpen(false)
    setPendingDeleteDoc(null)
  }, [deletingDocId])

  const closeDeleteCourseModal = useCallback(() => {
    if (isDeletingCourse) return
    setIsDeleteCourseModalOpen(false)
    setDeleteCourseError(null)
  }, [isDeletingCourse])

  const addFiles = useCallback((files) => {
    if (!files.length) return
    setSelectedFiles((prev) => mergeFileLists(prev, files))
  }, [])

  const removeFileAt = useCallback((index) => {
    setSelectedFiles((prev) => prev.filter((_, i) => i !== index))
  }, [])

  const handleUploadModalSubmit = useCallback(async () => {
    if (selectedFiles.length === 0 || isUploading) return
    if (!courseId) {
      setUploadError(t.coursePage.uploadMissingCourseId)
      return
    }

    setUploadError(null)
    setUploadSuccess(false)
    setIsUploading(true)
    const filesSnapshot = [...selectedFiles]
    const total = filesSnapshot.length
    setUploadProgress({ current: 0, total })

    try {
      const session = await fetchAuthSession()
      const idToken = session.tokens?.idToken?.toString()
      if (!idToken) {
        setUploadError(t.coursePage.uploadMissingSession)
        return
      }

      for (let i = 0; i < total; i++) {
        const file = filesSnapshot[i]
        if (file.size > MAX_UPLOAD_BYTES) {
          setUploadError(
            tx(t.coursePage.uploadFileTooLarge, {
              name: file.name,
              max: formatFileSize(MAX_UPLOAD_BYTES),
            }),
          )
          return
        }
        setUploadProgress({ current: i + 1, total })
        const fileType = file.type || 'application/octet-stream'
        const { upload_url: uploadUrl } = await getUploadUrl(
          courseId,
          file.name,
          fileType,
          file.size,
          idToken,
        )
        await uploadFileToS3(uploadUrl, file, fileType)
      }

      setSelectedFiles([])
      await loadDocuments()
      setUploadSuccess(true)
    } catch (err) {
      let message = t.coursePage.uploadError
      const apiMsg = err?.response?.data?.message
      if (typeof apiMsg === 'string' && apiMsg.trim()) {
        message = apiMsg.trim()
      } else if (typeof err?.message === 'string' && err.message.includes('VITE_API_URL')) {
        message = t.coursePage.uploadApiNotConfigured
      } else if (typeof err?.message === 'string' && err.message.trim()) {
        message = err.message.trim()
      }
      setUploadError(message)
    } finally {
      setIsUploading(false)
      setUploadProgress(null)
    }
  }, [courseId, isUploading, loadDocuments, selectedFiles, t, tx])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        await getCurrentUser()
        if (!cancelled) setAuthStatus('authed')
      } catch {
        if (!cancelled) setAuthStatus('guest')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    const tabParam = searchParams.get('tab')
    const setParam = searchParams.get('set')
    if (tabParam === 'questionSets' && activeTab !== 'questionSets') {
      setActiveTab('questionSets')
    } else if (tabParam !== 'questionSets' && activeTab !== 'materials') {
      setActiveTab('materials')
    }
    if (setParam && setParam !== selectedQuestionSet?.set_id) {
      setSelectedQuestionSet((prev) => (prev?.set_id === setParam ? prev : { set_id: setParam }))
    }
    if (!setParam && selectedQuestionSet?.set_id) {
      setSelectedQuestionSet(null)
      setSetQuestions([])
      setSetQuestionsError(null)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  useEffect(() => {
    if (!isUploadModalOpen) return undefined
    const onKeyDown = (e) => {
      if (e.key === 'Escape' && !isUploading) closeUploadModal()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [isUploadModalOpen, isUploading, closeUploadModal])

  useEffect(() => {
    const nextParams = new URLSearchParams(searchParams)
    if (activeTab === 'questionSets') {
      nextParams.set('tab', 'questionSets')
      if (selectedQuestionSet?.set_id) {
        nextParams.set('set', selectedQuestionSet.set_id)
      } else {
        nextParams.delete('set')
      }
    } else {
      nextParams.delete('tab')
      nextParams.delete('set')
    }
    if (nextParams.toString() !== searchParams.toString()) {
      setSearchParams(nextParams, {
        replace: true,
        state: location.state,
      })
    }
  }, [activeTab, selectedQuestionSet?.set_id, searchParams, setSearchParams, location.state])

  useEffect(() => {
    if (!isDeleteModalOpen) return undefined
    const onKeyDown = (e) => {
      if (e.key === 'Escape' && !deletingDocId) closeDeleteModal()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [isDeleteModalOpen, deletingDocId, closeDeleteModal])

  useEffect(() => {
    if (!quizStartedNotice) return undefined
    const timer = window.setTimeout(() => {
      setQuizStartedNotice(null)
    }, 3500)
    return () => window.clearTimeout(timer)
  }, [quizStartedNotice])

  useEffect(() => {
    if (!isGeneratingQuiz) {
      setQuizOverlayMessageIndex(0)
      return undefined
    }

    const intervalId = window.setInterval(() => {
      setQuizOverlayMessageIndex((prev) => (prev + 1) % 3)
    }, 1800)

    return () => window.clearInterval(intervalId)
  }, [isGeneratingQuiz])

  useEffect(() => {
    if (!uploadSuccess || !isUploadModalOpen) return undefined
    const timer = window.setTimeout(() => {
      closeUploadModal()
    }, 2200)
    return () => window.clearTimeout(timer)
  }, [uploadSuccess, isUploadModalOpen, closeUploadModal])

  if (authStatus === 'loading') {
    return (
      <main className="course-page" dir={dir} lang={lang}>
        <section className="course-page__loading">
          <p className="course-page__loading-text">{t.coursePage.loading}</p>
        </section>
      </main>
    )
  }

  if (authStatus === 'guest') {
    return <Navigate to="/" replace />
  }

  const materialsCount = documents.length
  const showDocList = !documentsLoading && !documentsError && documents.length > 0
  const showDocEmpty = !documentsLoading && !documentsError && documents.length === 0

  const handleDeleteClick = (doc, id) => {
    if (deletingDocId) return
    setDocumentsError(null)
    setDocumentsNotice(null)
    setQuizError(null)
    setPendingDeleteDoc({
      id: String(id),
      name: String(doc.original_file_name ?? doc.originalFileName ?? '—'),
    })
    setIsDeleteModalOpen(true)
  }

  const handleConfirmDelete = async () => {
    if (!pendingDeleteDoc?.id || !courseId || deletingDocId) return

    setDocumentsError(null)
    setDocumentsNotice(null)
    setDeletingDocId(pendingDeleteDoc.id)
    try {
      const session = await fetchAuthSession()
      const idToken = session.tokens?.idToken?.toString()
      if (!idToken) {
        throw new Error(t.coursePage.uploadMissingSession)
      }

      const result = await deleteDocument(courseId, pendingDeleteDoc.id, idToken)
      setDocuments((prev) =>
        prev.filter((doc, index) => {
          const itemId = String(doc.document_id ?? doc.documentId ?? `doc-${index}`)
          return itemId !== pendingDeleteDoc.id
        }),
      )
      setDocumentsNotice(result?.message || t.coursePage.deleteSuccess)
      setIsDeleteModalOpen(false)
      setPendingDeleteDoc(null)
    } catch (err) {
      let message = t.coursePage.deleteError
      const apiMsg = err?.response?.data?.message
      if (typeof apiMsg === 'string' && apiMsg.trim()) {
        message = apiMsg.trim()
      } else if (typeof err?.message === 'string' && err.message.includes('VITE_API_URL')) {
        message = t.coursePage.uploadApiNotConfigured
      } else if (typeof err?.message === 'string' && err.message.trim()) {
        message = err.message.trim()
      }
      setDocumentsError(message)
    } finally {
      setDeletingDocId(null)
    }
  }

  const toggleSelectionMode = () => {
    if (isGeneratingQuiz) return
    setQuizError(null)
    setSelectionMode((prev) => {
      if (prev) {
        setSelectedDocIds([])
      }
      return !prev
    })
  }

  const toggleDocSelection = (docId) => {
    if (!selectionMode || isGeneratingQuiz) return
    if (!eligibleDocIds.includes(docId)) return
    setSelectedDocIds((prev) =>
      prev.includes(docId) ? prev.filter((existingId) => existingId !== docId) : [...prev, docId],
    )
  }

  const handleGenerateQuiz = async () => {
    if (!courseId || selectedDocIds.length === 0 || isGeneratingQuiz) return

    setDocumentsError(null)
    setDocumentsNotice(null)
    setQuizStartedNotice(null)
    setQuizError(null)
    setIsGeneratingQuiz(true)
    try {
      const session = await fetchAuthSession()
      const idToken = session.tokens?.idToken?.toString()
      if (!idToken) {
        throw new Error(t.coursePage.uploadMissingSession)
      }
      await generateQuiz(courseId, selectedDocIds, idToken)
      setSelectionMode(false)
      setSelectedDocIds([])
      setQuizStartedNotice(t.coursePage.success)
      await loadDocuments()
      await loadQuestionSets()
      setQuestionSetsNotice(null)
      setQuestionSetsError(null)
      setSetQuestionsError(null)
      setActiveTab('questionSets')
    } catch (err) {
      let message = t.coursePage.quizGenerationFailed
      const apiMsg = err?.response?.data?.message
      if (typeof apiMsg === 'string' && apiMsg.trim()) {
        message = apiMsg.trim()
      } else if (typeof err?.message === 'string' && err.message.includes('VITE_API_URL')) {
        message = t.coursePage.uploadApiNotConfigured
      } else if (typeof err?.message === 'string' && err.message.trim()) {
        message = err.message.trim()
      }
      setQuizError(message)
    } finally {
      setIsGeneratingQuiz(false)
    }
  }

  const quizOverlayMessages = [
    t.coursePage.quizOverlayMessage1,
    t.coursePage.quizOverlayMessage2,
    t.coursePage.quizOverlayMessage3,
  ]

  const formatDifficultySummary = (setItem) => {
    const breakdown = setItem?.difficulty_breakdown ?? setItem?.difficultyBreakdown ?? {}
    const easy = Number(breakdown.easy ?? 0)
    const medium = Number(breakdown.medium ?? 0)
    const hard = Number(breakdown.hard ?? 0)
    const derivedTotal = easy + medium + hard
    const total = Number(setItem?.question_count ?? setItem?.questionCount ?? derivedTotal ?? 0)
    return tx(t.coursePage.questionSetDifficultySummary, {
      total,
      easy,
      medium,
      hard,
      easyLabel: t.coursePage.difficultyEasy,
      mediumLabel: t.coursePage.difficultyMedium,
      hardLabel: t.coursePage.difficultyHard,
    })
  }

  const handleOpenSet = (setItem) => {
    setQuestionMode('review')
    setPracticeAnswers({})
    setSetQuestionsError(null)
    setSelectedQuestionSet(setItem)
  }

  const handleDeleteSet = async () => {
    if (!setPendingDelete?.set_id || !courseId || isDeletingSet) return
    setIsDeletingSet(true)
    setQuestionSetsError(null)
    try {
      const session = await fetchAuthSession()
      const idToken = session.tokens?.idToken?.toString()
      if (!idToken) throw new Error(t.coursePage.uploadMissingSession)
      await deleteQuestionSet(courseId, setPendingDelete.set_id, idToken)
      setQuestionSets((prev) => prev.filter((setItem) => setItem.set_id !== setPendingDelete.set_id))
      if (selectedQuestionSet?.set_id === setPendingDelete.set_id) {
        setSelectedQuestionSet(null)
        setSetQuestions([])
      }
      setQuestionSetsNotice(t.coursePage.deleteSetSuccess)
      setSetPendingDelete(null)
    } catch (err) {
      const apiMsg = err?.response?.data?.message
      setQuestionSetsError(
        typeof apiMsg === 'string' && apiMsg.trim() ? apiMsg.trim() : t.coursePage.deleteSetError,
      )
    } finally {
      setIsDeletingSet(false)
    }
  }

  return (
    <main
      className={`course-page ${selectedDocIds.length > 0 ? 'course-page--with-action-bar' : ''}`}
      dir={dir}
      lang={lang}
    >
      <div className="course-page__top-bar">
        <button
          type="button"
          className="course-page__back-btn"
          onClick={() => navigate('/home')}
        >
          {t.coursePage.backToDashboard}
        </button>
        <div className="course-page__lang-switch" role="group" aria-label={t.common.switchLanguage}>
          <button
            type="button"
            className={`course-page__lang-btn ${lang === 'he' ? 'course-page__lang-btn--active' : ''}`}
            onClick={() => setLang('he')}
          >
            {t.common.langHe}
          </button>
          <button
            type="button"
            className={`course-page__lang-btn ${lang === 'en' ? 'course-page__lang-btn--active' : ''}`}
            onClick={() => setLang('en')}
          >
            {t.common.langEn}
          </button>
        </div>
      </div>

      <header className="course-page__banner">
        <div className="course-page__banner-inner">
          <h1 className="course-page__title">{displayCourseName}</h1>
          <p className="course-page__materials-stat" aria-live="polite">
            {tx(t.coursePage.materialsCountStat, { count: materialsCount })}
          </p>
        </div>
      </header>

      <div className="course-page__body">
        <nav
          className="course-page__inner-sidebar"
          aria-label={t.coursePage.courseInnerNavAria}
        >
          <div className="course-page__inner-nav">
            <button
              type="button"
              className={`course-page__inner-nav-item ${
                activeTab === 'materials' ? 'course-page__inner-nav-item--active' : ''
              }`}
              onClick={() => setActiveTab('materials')}
              aria-current={activeTab === 'materials' ? 'page' : undefined}
            >
              {t.coursePage.tabMaterials}
            </button>
            <button
              type="button"
              className={`course-page__inner-nav-item ${
                activeTab === 'questionSets' ? 'course-page__inner-nav-item--active' : ''
              }`}
              onClick={() => setActiveTab('questionSets')}
              aria-current={activeTab === 'questionSets' ? 'page' : undefined}
            >
              {t.coursePage.tabQuestionSets}
            </button>
          </div>
        </nav>

        <div className="course-page__main">
          {activeTab === 'materials' ? (
            <section aria-label={t.coursePage.materialsSectionLabel}>
              <div className="course-page__materials-header">
                <h2 className="course-page__materials-heading course-page__materials-heading--panel">
                  {t.coursePage.materialsHeading}
                </h2>
                <div className="course-page__materials-actions">
                  <button
                    type="button"
                    className={`course-page__quiz-btn ${selectionMode ? 'course-page__quiz-btn--active' : ''}`}
                    onClick={toggleSelectionMode}
                    disabled={isGeneratingQuiz}
                  >
                    <span className="course-page__quiz-btn-icon" aria-hidden>
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                        <path
                          d="M12 3.75l1.64 3.323 3.668.533-2.654 2.587.626 3.652L12 12.12l-3.28 1.725.626-3.652-2.654-2.587 3.668-.533L12 3.75Zm6.25 10 1.1 2.23 2.46.357-1.78 1.736.42 2.451-2.2-1.157-2.2 1.157.42-2.451-1.78-1.736 2.46-.357 1.1-2.23Zm-12.5 0 1.1 2.23 2.46.357-1.78 1.736.42 2.451-2.2-1.157-2.2 1.157.42-2.451-1.78-1.736 2.46-.357 1.1-2.23Z"
                          fill="currentColor"
                        />
                      </svg>
                    </span>
                    {selectionMode ? t.coursePage.quizSelectionCancel : t.coursePage.newPracticeQuiz}
                  </button>
                  <button
                    type="button"
                    className="course-page__upload-btn"
                    disabled={isUploading || isGeneratingQuiz}
                    onClick={() => {
                      setUploadError(null)
                      setUploadSuccess(false)
                      setIsUploadModalOpen(true)
                    }}
                  >
                    {t.coursePage.uploadMaterial}
                  </button>
                  <button
                    type="button"
                    className="course-page__modal-cancel course-page__delete-course-btn"
                    onClick={() => {
                      setDeleteCourseError(null)
                      setIsDeleteCourseModalOpen(true)
                    }}
                  >
                    {t.coursePage.deleteCourseLabel}
                  </button>
                </div>
              </div>

              {documentsLoading ? (
                <div className="course-page__documents-skeleton" aria-busy="true">
                  <div className="course-page__documents-skeleton-row" />
                  <div className="course-page__documents-skeleton-row" />
                  <div className="course-page__documents-skeleton-row" />
                  <p className="course-page__documents-state">{t.coursePage.documentsLoading}</p>
                </div>
              ) : null}

              {documentsError && !documentsLoading ? (
                <p className="course-page__documents-error" role="alert">
                  {documentsError}
                </p>
              ) : null}

              {quizError && !documentsLoading ? (
                <div className="course-page__quiz-error" role="alert">
                  <p className="course-page__quiz-error-text">{quizError}</p>
                  <button
                    type="button"
                    className="course-page__quiz-error-retry"
                    onClick={handleGenerateQuiz}
                    disabled={isGeneratingQuiz || selectedDocIds.length === 0}
                  >
                    {t.coursePage.tryAgain}
                  </button>
                </div>
              ) : null}

              {documentsNotice && !documentsLoading ? (
                <p className="course-page__documents-notice" role="status">
                  {documentsNotice}
                </p>
              ) : null}

              {quizStartedNotice && !documentsLoading ? (
                <p className="course-page__documents-notice" role="status">
                  {quizStartedNotice}
                </p>
              ) : null}

              {showDocEmpty ? (
                <p className="course-page__materials-empty">{t.coursePage.documentsListEmpty}</p>
              ) : null}

              {showDocList ? (
                <ul
                  className="course-page__doc-list"
                  aria-label={t.coursePage.documentsListAriaLabel}
                >
                  {documents.map((doc, index) => {
                    const id = doc.document_id ?? doc.documentId ?? `doc-${index}`
                    const name = doc.original_file_name ?? doc.originalFileName ?? '—'
                    const created = doc.created_at ?? doc.createdAt
                    const status = doc.processing_status ?? doc.processingStatus ?? ''
                    const normalizedStatus = normalizeProcessingStatus(status)
                    const isInteractive = QUIZ_ELIGIBLE_STATUSES.has(normalizedStatus)
                    const deletingThisDoc = deletingDocId === String(id)
                    const docId = String(id)
                    const isSelected = selectedDocIds.includes(docId)
                    return (
                      <li
                        key={String(id)}
                        className={`course-page__doc-card ${
                          !isInteractive ? 'course-page__doc-card--processing' : ''
                        }`}
                      >
                        {selectionMode ? (
                          <label className="course-page__doc-select" aria-label={tx(t.coursePage.quizSelectDocAria, { name })}>
                            <input
                              type="checkbox"
                              className="course-page__doc-select-input"
                              checked={isSelected}
                              onChange={() => toggleDocSelection(docId)}
                              disabled={isGeneratingQuiz || !isInteractive}
                            />
                            <span className="course-page__doc-select-checkmark" aria-hidden />
                          </label>
                        ) : null}
                        <div className="course-page__doc-card-main">
                          <span className="course-page__doc-card-name-row">
                            <span className="course-page__doc-card-name" title={String(name)}>
                              {String(name)}
                            </span>
                          </span>
                          <span className="course-page__doc-card-date">
                            {formatDocumentDate(created, lang)}
                          </span>
                        </div>
                        {!isInteractive ? <span className="mini-spinner" aria-hidden /> : null}
                        <div className="course-page__doc-card-actions">
                          <button
                            type="button"
                            className="course-page__doc-delete-btn"
                            disabled={Boolean(deletingDocId) || isGeneratingQuiz}
                            onClick={() => handleDeleteClick(doc, id)}
                            aria-label={tx(t.coursePage.deleteDocumentAria, { name })}
                          >
                            <span className="course-page__doc-delete-icon" aria-hidden>
                              <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                                <path
                                  d="M9 3.75h6m-7.5 3h9m-7.5 3.75v7.5m3-7.5v7.5m4.875-10.5-.662 9.272A2.25 2.25 0 0 1 13.97 21h-3.94a2.25 2.25 0 0 1-2.243-2.028L7.125 7.5"
                                  stroke="currentColor"
                                  strokeWidth="1.5"
                                  strokeLinecap="round"
                                  strokeLinejoin="round"
                                />
                              </svg>
                            </span>
                            {deletingThisDoc ? t.coursePage.deleteDeleting : t.coursePage.deleteLabel}
                          </button>
                        </div>
                      </li>
                    )
                  })}
                </ul>
              ) : null}
            </section>
          ) : null}
          {activeTab === 'questionSets' ? (
            <section aria-label={t.coursePage.questionSetsSectionLabel}>
              <div className="course-page__materials-header">
                <h2 className="course-page__materials-heading course-page__materials-heading--panel">
                  {t.coursePage.questionSetsHeading}
                </h2>
              </div>
              {questionSetsError ? (
                <p className="course-page__documents-error" role="alert">
                  {questionSetsError}
                </p>
              ) : null}
              {questionSetsNotice ? (
                <p className="course-page__documents-notice" role="status">
                  {questionSetsNotice}
                </p>
              ) : null}
              {!selectedQuestionSet ? (
                <>
                  {questionSetsLoading ? (
                    <div className="course-page__documents-skeleton" aria-busy="true">
                      <div className="course-page__documents-skeleton-row" />
                      <div className="course-page__documents-skeleton-row" />
                    </div>
                  ) : null}
                  {!questionSetsLoading && questionSets.length === 0 ? (
                    <p className="course-page__materials-empty">{t.coursePage.questionSetsEmpty}</p>
                  ) : null}
                  {!questionSetsLoading && questionSets.length > 0 ? (
                    <ul className="course-page__set-list">
                      {questionSets.map((setItem) => (
                        <li key={setItem.set_id} className="course-page__set-card">
                          <button
                            type="button"
                            className="course-page__set-card-open"
                            onClick={() => handleOpenSet(setItem)}
                          >
                            <p className="course-page__set-card-title">
                              {setItem.name || setItem.set_name || t.coursePage.questionSetUntitled}
                            </p>
                            <p className="course-page__set-card-meta">
                              {formatDocumentDate(setItem.created_at, lang)}
                            </p>
                            <p className="course-page__set-card-meta">{formatDifficultySummary(setItem)}</p>
                            {Array.isArray(setItem.source_document_names) &&
                            setItem.source_document_names.length > 0 ? (
                              <p className="course-page__set-card-meta">
                                {setItem.source_document_names.join(', ')}
                              </p>
                            ) : null}
                          </button>
                          <button
                            type="button"
                            className="course-page__set-delete-btn"
                            onClick={() => setSetPendingDelete(setItem)}
                            aria-label={tx(t.coursePage.deleteSetAria, {
                              name: setItem.name || setItem.set_name || t.coursePage.questionSetUntitled,
                            })}
                          >
                            <span aria-hidden>
                              <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
                                <path
                                  d="M9 3.75h6m-7.5 3h9m-7.5 3.75v7.5m3-7.5v7.5m4.875-10.5-.662 9.272A2.25 2.25 0 0 1 13.97 21h-3.94a2.25 2.25 0 0 1-2.243-2.028L7.125 7.5"
                                  stroke="currentColor"
                                  strokeWidth="1.5"
                                  strokeLinecap="round"
                                  strokeLinejoin="round"
                                />
                              </svg>
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </>
              ) : (
                <div className="course-page__set-detail">
                  <div className="course-page__set-detail-top">
                    <button
                      type="button"
                      className="course-page__back-btn"
                      onClick={() => {
                        setSelectedQuestionSet(null)
                        setSetQuestions([])
                      }}
                    >
                      {t.coursePage.backToSets}
                    </button>
                    <div className="course-page__mode-switch" role="group" aria-label={t.coursePage.modeSwitchAria}>
                      <button
                        type="button"
                        className={`course-page__mode-btn ${questionMode === 'review' ? 'course-page__mode-btn--active' : ''}`}
                        onClick={() => setQuestionMode('review')}
                      >
                        {t.coursePage.reviewMode}
                      </button>
                      <button
                        type="button"
                        className={`course-page__mode-btn ${questionMode === 'practice' ? 'course-page__mode-btn--active' : ''}`}
                        onClick={() => setQuestionMode('practice')}
                      >
                        {t.coursePage.practiceMode}
                      </button>
                    </div>
                  </div>
                  {setQuestionsLoading ? (
                    <p className="course-page__documents-state">{t.coursePage.questionSetLoading}</p>
                  ) : null}
                  {setQuestionsError ? (
                    <p className="course-page__documents-error" role="alert">
                      {setQuestionsError}
                    </p>
                  ) : null}
                  {!setQuestionsLoading ? (
                    <ol className="course-page__question-list">
                      {setQuestions.map((question, index) => {
                        const qid = String(question.question_id ?? question.questionId ?? `q-${index}`)
                        const selectedIndex = practiceAnswers[qid]
                        const shouldReveal = questionMode === 'review' || selectedIndex !== undefined
                        return (
                          <li key={qid} className="course-page__question-card">
                            <p className="course-page__question-title">{question.question}</p>
                            <ul className="course-page__question-options">
                              {(Array.isArray(question.options) ? question.options : []).map((opt, optIndex) => {
                                const isCorrect = Number(question.correct_index) === optIndex
                                return (
                                  <li key={`${qid}-${optIndex}`}>
                                    <button
                                      type="button"
                                      className={`course-page__question-option ${
                                        shouldReveal && isCorrect ? 'course-page__question-option--correct' : ''
                                      }`}
                                      onClick={() =>
                                        setPracticeAnswers((prev) => ({
                                          ...prev,
                                          [qid]: optIndex,
                                        }))
                                      }
                                      disabled={questionMode === 'review' || selectedIndex !== undefined}
                                    >
                                      {String.fromCharCode(65 + optIndex)}. {opt}
                                    </button>
                                  </li>
                                )
                              })}
                            </ul>
                            {shouldReveal ? (
                              <>
                                <p className="course-page__question-answer">
                                  {t.coursePage.correctAnswer}:{' '}
                                  {(question.options || [])[Number(question.correct_index)]}
                                </p>
                                <p className="course-page__question-explanation">
                                  {t.coursePage.aiExplanation}: {question.explanation}
                                </p>
                              </>
                            ) : null}
                          </li>
                        )
                      })}
                    </ol>
                  ) : null}
                </div>
              )}
            </section>
          ) : null}
        </div>
      </div>

      {isUploadModalOpen ? (
        <div
          className="course-page__modal-backdrop"
          role="presentation"
          onClick={() => {
            if (!isUploading) closeUploadModal()
          }}
        >
          <section
            className="course-page__modal course-page__modal--upload"
            role="dialog"
            aria-modal="true"
            aria-labelledby="course-upload-modal-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 id="course-upload-modal-title" className="course-page__modal-title">
              {t.coursePage.uploadModalTitle}
            </h2>
            <p className="course-page__modal-subtitle">{t.coursePage.uploadModalSubtitle}</p>

            {uploadSuccess ? (
              <p className="course-page__upload-feedback course-page__upload-feedback--success" role="status">
                {t.coursePage.uploadSuccess}
              </p>
            ) : null}

            {uploadError ? (
              <p className="course-page__upload-feedback course-page__upload-feedback--error" role="alert">
                {uploadError}
              </p>
            ) : null}

            {isUploading && uploadProgress ? (
              <p className="course-page__upload-feedback course-page__upload-feedback--progress" aria-live="polite">
                {tx(t.coursePage.uploadProgress, {
                  current: uploadProgress.current,
                  total: uploadProgress.total,
                })}
              </p>
            ) : null}

            {!uploadSuccess ? (
              <>
                <input
                  ref={fileInputRef}
                  type="file"
                  className="course-page__upload-input"
                  multiple
                  tabIndex={-1}
                  disabled={isUploading}
                  onChange={(e) => {
                    const { files } = e.target
                    if (files?.length) addFiles(Array.from(files))
                    e.target.value = ''
                  }}
                />

                <div
                  className={`course-page__upload-dropzone ${
                    isDraggingOverDropzone ? 'course-page__upload-dropzone--active' : ''
                  } ${isUploading ? 'course-page__upload-dropzone--disabled' : ''}`}
                  role="button"
                  tabIndex={isUploading ? -1 : 0}
                  aria-disabled={isUploading}
                  aria-label={t.coursePage.uploadDropHint}
                  onClick={() => {
                    if (!isUploading) fileInputRef.current?.click()
                  }}
                  onKeyDown={(e) => {
                    if (isUploading) return
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      fileInputRef.current?.click()
                    }
                  }}
                  onDragEnter={(e) => {
                    if (isUploading) return
                    e.preventDefault()
                    e.stopPropagation()
                    dragDepthRef.current += 1
                    setIsDraggingOverDropzone(true)
                  }}
                  onDragOver={(e) => {
                    if (isUploading) return
                    e.preventDefault()
                    e.stopPropagation()
                  }}
                  onDragLeave={(e) => {
                    if (isUploading) return
                    e.preventDefault()
                    e.stopPropagation()
                    dragDepthRef.current -= 1
                    if (dragDepthRef.current <= 0) {
                      dragDepthRef.current = 0
                      setIsDraggingOverDropzone(false)
                    }
                  }}
                  onDrop={(e) => {
                    if (isUploading) return
                    e.preventDefault()
                    e.stopPropagation()
                    dragDepthRef.current = 0
                    setIsDraggingOverDropzone(false)
                    const dropped = Array.from(e.dataTransfer?.files || [])
                    addFiles(dropped)
                  }}
                >
                  <span className="course-page__upload-dropzone-icon" aria-hidden>
                    <svg width="40" height="40" viewBox="0 0 24 24" fill="none">
                      <path
                        d="M12 4v12m0 0l-4-4m4 4l4-4M5 20h14"
                        stroke="currentColor"
                        strokeWidth="1.5"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  </span>
                  <p className="course-page__upload-dropzone-hint">{t.coursePage.uploadDropHint}</p>
                </div>

                {selectedFiles.length > 0 ? (
                  <div className="course-page__upload-files">
                    <p className="course-page__upload-files-label">{t.coursePage.uploadSelectedHeading}</p>
                    <ul className="course-page__upload-files-list">
                      {selectedFiles.map((file, index) => (
                        <li key={fileKey(file)} className="course-page__upload-files-item">
                          <span className="course-page__upload-files-name" title={file.name}>
                            {file.name}
                          </span>
                          <span className="course-page__upload-files-size">{formatFileSize(file.size)}</span>
                          <button
                            type="button"
                            className="course-page__upload-files-remove"
                            disabled={isUploading}
                            onClick={(e) => {
                              e.stopPropagation()
                              removeFileAt(index)
                            }}
                            aria-label={tx(t.coursePage.uploadRemoveFileAria, { name: file.name })}
                          >
                            ×
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </>
            ) : null}

            <div className="course-page__modal-actions">
              <button
                type="button"
                className="course-page__modal-cancel"
                disabled={isUploading}
                onClick={closeUploadModal}
              >
                {t.home.cancel}
              </button>
              {!uploadSuccess ? (
                <button
                  type="button"
                  className="course-page__modal-submit"
                  disabled={selectedFiles.length === 0 || isUploading}
                  onClick={handleUploadModalSubmit}
                >
                  {isUploading ? t.coursePage.uploadUploading : t.coursePage.uploadSubmit}
                </button>
              ) : null}
            </div>
          </section>
        </div>
      ) : null}

      {isDeleteModalOpen ? (
        <div
          className="course-page__modal-backdrop"
          role="presentation"
          onClick={closeDeleteModal}
        >
          <section
            className="course-page__modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="course-delete-modal-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 id="course-delete-modal-title" className="course-page__modal-title">
              {t.coursePage.deleteModalTitle}
            </h2>
            <p className="course-page__modal-subtitle">
              {tx(t.coursePage.deleteConfirm, { name: pendingDeleteDoc?.name || '—' })}
            </p>
            <div className="course-page__modal-actions">
              <button
                type="button"
                className="course-page__modal-cancel"
                disabled={Boolean(deletingDocId)}
                onClick={closeDeleteModal}
              >
                {t.home.cancel}
              </button>
              <button
                type="button"
                className="course-page__modal-submit course-page__modal-submit--danger"
                disabled={Boolean(deletingDocId)}
                onClick={handleConfirmDelete}
              >
                {deletingDocId ? t.coursePage.deleteDeleting : t.coursePage.deleteConfirmCta}
              </button>
            </div>
          </section>
        </div>
      ) : null}
      {isDeleteCourseModalOpen ? (
        <div
          className="course-page__modal-backdrop"
          role="presentation"
          onClick={closeDeleteCourseModal}
        >
          <section
            className="course-page__modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="course-delete-course-modal-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 id="course-delete-course-modal-title" className="course-page__modal-title">
              {t.coursePage.deleteCourseModalTitle}
            </h2>
            <p className="course-page__modal-subtitle">
              {tx(t.coursePage.deleteCoursePrompt, { name: displayCourseName })}
            </p>
            {deleteCourseError ? (
              <p className="course-page__documents-error" role="alert">
                {deleteCourseError}
              </p>
            ) : null}
            <div className="course-page__modal-actions">
              <button
                type="button"
                className="course-page__modal-cancel"
                disabled={isDeletingCourse}
                onClick={closeDeleteCourseModal}
              >
                {t.home.cancel}
              </button>
              <button
                type="button"
                className="course-page__modal-submit course-page__modal-submit--danger"
                disabled={isDeletingCourse}
                onClick={async () => {
                  if (!courseId || isDeletingCourse) return
                  try {
                    setIsDeletingCourse(true)
                    setDeleteCourseError(null)
                    const session = await fetchAuthSession()
                    const idToken = session.tokens?.idToken?.toString()
                    if (!idToken) {
                      throw new Error(t.coursePage.uploadMissingSession)
                    }
                    await deleteCourse(courseId, idToken)
                    navigate('/home', { replace: true })
                  } catch (err) {
                    const apiMsg = err?.response?.data?.message
                    if (typeof apiMsg === 'string' && apiMsg.trim()) {
                      setDeleteCourseError(apiMsg.trim())
                    } else if (typeof err?.message === 'string' && err.message.trim()) {
                      setDeleteCourseError(err.message.trim())
                    } else {
                      setDeleteCourseError(t.coursePage.deleteCourseError)
                    }
                  } finally {
                    setIsDeletingCourse(false)
                  }
                }}
              >
                {isDeletingCourse
                  ? t.coursePage.deleteCourseDeleting
                  : t.coursePage.deleteCourseConfirm}
              </button>
            </div>
          </section>
        </div>
      ) : null}
      {setPendingDelete ? (
        <div
          className="course-page__modal-backdrop"
          role="presentation"
          onClick={() => {
            if (!isDeletingSet) setSetPendingDelete(null)
          }}
        >
          <section
            className="course-page__modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="course-delete-set-modal-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 id="course-delete-set-modal-title" className="course-page__modal-title">
              {t.coursePage.deleteSet}
            </h2>
            <p className="course-page__modal-subtitle">
              {tx(t.coursePage.deleteSetConfirm, {
                name: setPendingDelete.name || setPendingDelete.set_name || t.coursePage.questionSetUntitled,
              })}
            </p>
            <div className="course-page__modal-actions">
              <button
                type="button"
                className="course-page__modal-cancel"
                onClick={() => setSetPendingDelete(null)}
                disabled={isDeletingSet}
              >
                {t.home.cancel}
              </button>
              <button
                type="button"
                className="course-page__modal-submit course-page__modal-submit--danger"
                onClick={handleDeleteSet}
                disabled={isDeletingSet}
              >
                {isDeletingSet ? t.coursePage.deleteDeleting : t.coursePage.deleteSet}
              </button>
            </div>
          </section>
        </div>
      ) : null}
      {selectedDocIds.length > 0 ? (
        <div className="course-page__quiz-action-bar" role="status" aria-live="polite">
          <p className="course-page__quiz-action-count">
            {tx(t.coursePage.quizSelectedCount, { count: selectedDocIds.length })}
          </p>
          <button
            type="button"
            className="course-page__quiz-generate-btn"
            onClick={handleGenerateQuiz}
            disabled={isGeneratingQuiz || selectedDocIds.length === 0}
          >
            {isGeneratingQuiz ? t.coursePage.quizGenerating : t.coursePage.quizGenerate}
          </button>
        </div>
      ) : null}
      {isGeneratingQuiz ? (
        <div className="quiz-loading-overlay" role="status" aria-live="polite" aria-busy="true">
          <div className="quiz-loading-overlay__content">
            <span className="quiz-loading-overlay__spinner" aria-hidden />
            <p className="quiz-loading-overlay__title">{t.coursePage.quizLoadingTitle}</p>
            <p className="quiz-loading-overlay__message">
              {quizOverlayMessages[quizOverlayMessageIndex]}
            </p>
          </div>
        </div>
      ) : null}
    </main>
  )
}
