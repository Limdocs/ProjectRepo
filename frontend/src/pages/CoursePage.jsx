import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Navigate, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { fetchAuthSession, getCurrentUser } from 'aws-amplify/auth'
import './CoursePage.css'
import TopicScoreChart from '../components/TopicScoreChart.jsx'
import { useLanguageControl } from '../language-control/LanguageControlProvider.jsx'
import {
  deleteAttempt,
  deleteCourse,
  getAttemptAnswers,
  getCourseAttempts,
  getCourseProgress,
  getUserCourses,
  submitAttempt,
} from '../services/coursesService.js'
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
import { resolveProgressTopics } from '../utils/topicScoring.js'

const FINAL_PROCESSING_STATUSES = new Set(['READY', 'FAILED', 'ERROR'])
const QUIZ_ELIGIBLE_STATUSES = new Set(['READY', 'FAILED'])
const DOCUMENT_POLL_INTERVAL_MS = 7000
const QUIZ_POLL_INTERVAL_MS = 4000
const QUIZ_POLL_TIMEOUT_MS = 5 * 60 * 1000
const QUIZ_GENERATE_COUNT_OPTIONS = [5, 10, 15, 20]

function sleep(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms)
  })
}

function getQuizJobStatus(documents, pendingDocIds) {
  const pending = new Set(pendingDocIds.map((id) => String(id)))
  if (pending.size === 0) {
    return { state: 'success' }
  }

  let anyGenerating = false
  let anyReady = false

  for (const doc of documents) {
    const id = String(doc.document_id ?? doc.documentId ?? '')
    if (!pending.has(id)) continue
    const status = normalizeProcessingStatus(doc.processing_status ?? doc.processingStatus)
    if (status === 'GENERATING') {
      anyGenerating = true
    } else if (status === 'FAILED' || status === 'ERROR') {
      const reason = doc.failure_reason ?? doc.failureReason
      return {
        state: 'failed',
        failureReason:
          typeof reason === 'string' && reason.trim() ? reason.trim() : null,
      }
    } else if (status === 'READY') {
      anyReady = true
    }
  }

  if (!anyGenerating && anyReady) {
    return { state: 'success' }
  }
  return { state: 'running' }
}

async function waitForQuizCompletion(pendingDocIds, loadDocuments, loadQuestionSets, labels) {
  const startedAt = Date.now()
  while (Date.now() - startedAt < QUIZ_POLL_TIMEOUT_MS) {
    const docs = await loadDocuments({ silent: true })
    await loadQuestionSets({ silent: true })
    if (Array.isArray(docs)) {
      const jobStatus = getQuizJobStatus(docs, pendingDocIds)
      if (jobStatus.state === 'success') {
        return { ok: true }
      }
      if (jobStatus.state === 'failed') {
        return {
          ok: false,
          message: jobStatus.failureReason || labels.quizGenerationFailed,
        }
      }
    }
    await sleep(QUIZ_POLL_INTERVAL_MS)
  }
  return { ok: false, message: labels.quizGenerationTimeout, timeout: true }
}

function documentStatusLabel(status, labels) {
  const normalized = normalizeProcessingStatus(status)
  const labelMap = {
    UPLOADED: labels.statusUploaded,
    PROCESSING: labels.statusProcessing,
    GENERATING: labels.statusGenerating,
    READY: labels.statusReady,
    GENERATED: labels.statusGenerated,
    FAILED: labels.statusFailed,
    ERROR: labels.statusFailed,
  }
  return labelMap[normalized] ?? (normalized || '—')
}

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

function resolveQuestionSetLabel(setId, questionSets) {
  const match = questionSets.find((s) => s.set_id === setId)
  return match?.title ?? match?.name ?? match?.set_name ?? setId ?? '—'
}

function resolveQuizLanguage(setItem) {
  const raw = setItem?.quiz_language ?? setItem?.quizLanguage ?? 'he'
  return String(raw).toLowerCase() === 'en' ? 'en' : 'he'
}

function getOptionLabel(index, quizLanguage) {
  const hebrewLabels = ['א', 'ב', 'ג', 'ד']
  const englishLabels = ['A', 'B', 'C', 'D']
  return quizLanguage === 'en'
    ? englishLabels[index] ?? String.fromCharCode(65 + index)
    : hebrewLabels[index] ?? String(index + 1)
}

function resolveQuizContentDir(setItem) {
  return resolveQuizLanguage(setItem) === 'en' ? 'ltr' : 'rtl'
}

function resolveSetQuestionCount(setItem) {
  const actual = Number(setItem?.question_count ?? setItem?.questionCount ?? 0)
  if (actual > 0) return actual
  const requested = Number(
    setItem?.requested_question_count ?? setItem?.requestedQuestionCount ?? 0,
  )
  return requested > 0 ? requested : 0
}

function quizLanguageBadgeLabel(quizLang, labels) {
  return quizLang === 'en' ? labels.quizLanguageBadgeEn : labels.quizLanguageBadgeHe
}

function formatTimeSpent(seconds, labels, txFn) {
  if (seconds == null || seconds < 0) return '—'
  const mins = Math.floor(seconds / 60)
  const secs = seconds % 60
  if (mins === 0) return txFn(labels.attemptTimeSecondsOnly, { seconds: secs })
  return txFn(labels.attemptTimeMinutesSeconds, { minutes: mins, seconds: secs })
}

function getScoreTier(score) {
  if (score === undefined || score === null || score === '—') return null
  const numericScore = Number(score)
  if (Number.isNaN(numericScore)) return null
  if (numericScore >= 80) return 'high'
  if (numericScore >= 60) return 'medium'
  return 'low'
}

const WEAKNESS_DIFFICULTIES = ['Easy', 'Medium', 'Hard']

const WEAKNESS_DIFFICULTY_LABEL_KEYS = {
  Easy: 'weaknessDifficultyEasy',
  Medium: 'weaknessDifficultyMedium',
  Hard: 'weaknessDifficultyHard',
}

const WEAKNESS_STATUS_LABEL_KEYS = {
  weak: 'weaknessStatusWeak',
  medium: 'weaknessStatusMedium',
  strong: 'weaknessStatusStrong',
}

const WEAKNESS_STATUS_CHIP_CLASS = {
  weak: 'course-page__weakness-status-chip--low',
  medium: 'course-page__weakness-status-chip--mid',
  strong: 'course-page__weakness-status-chip--high',
}

function normalizeTopics(doc) {
  const raw = doc?.topics
  return Array.isArray(raw) ? raw.filter((t) => t?.en || t?.he) : []
}

function getTopicLabel(topic, lang) {
  if (lang === 'he') return (topic.he || topic.en || '').trim() || topic.en
  return (topic.en || topic.he || '').trim()
}

function buildTopicCatalog(documents) {
  const byEn = new Map()
  for (const doc of documents) {
    for (const topic of normalizeTopics(doc)) {
      const en = (topic.en || '').trim()
      if (en && !byEn.has(en)) byEn.set(en, topic)
    }
  }
  return byEn
}

function resolveMatrixTopicLabel(englishKey, catalog, lang) {
  const topic = catalog.get(englishKey)
  if (topic) return getTopicLabel(topic, lang)
  return englishKey
}

function masteryPercent(correct, total) {
  const t = Number(total) || 0
  const c = Number(correct) || 0
  if (t <= 0) return null
  return Math.round((c / t) * 100)
}

function getMasteryTierClass(pct) {
  if (pct == null) return 'course-page__mastery-fill--none'
  if (pct < 50) return 'course-page__mastery-fill--low'
  if (pct < 80) return 'course-page__mastery-fill--mid'
  return 'course-page__mastery-fill--high'
}

function mapAttemptAnswersToPractice(questions, answersByQuestionId) {
  const mapped = {}
  for (const question of questions) {
    const qid = String(question.question_id ?? question.questionId ?? '')
    if (!qid) continue
    const raw = answersByQuestionId[qid]
    if (raw === undefined || raw === null) continue
    mapped[qid] = Number(raw)
  }
  return mapped
}

function QuestionSetPreviewCard({
  setItem,
  labels,
  lang,
  txFn,
  formatDocumentDateFn,
  formatDifficultySummaryFn,
  onStartAttempt,
  onDelete,
  isStarting,
  startDisabled,
  deleteAriaLabel,
}) {
  const title = setItem.name || setItem.set_name || labels.questionSetUntitled
  const quizLang = resolveQuizLanguage(setItem)
  const questionCount = resolveSetQuestionCount(setItem)

  return (
    <article className="course-page__set-card course-page__set-preview">
      <div className="course-page__set-preview-toolbar">
        <button
          type="button"
          className="course-page__set-delete-btn course-page__set-delete-btn--inline"
          onClick={onDelete}
          aria-label={deleteAriaLabel}
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
      </div>
      <div className="course-page__set-preview-body">
        <p className="course-page__set-card-title">{title}</p>
        <div className="course-page__set-meta-badges">
          <span className="course-page__set-meta-badge course-page__set-meta-badge--language">
            {quizLanguageBadgeLabel(quizLang, labels)}
          </span>
          <span className="course-page__set-meta-badge course-page__set-meta-badge--count">
            {txFn(labels.questionSetMetaCount, { count: questionCount })}
          </span>
        </div>
        <p className="course-page__set-card-meta">{formatDocumentDateFn(setItem.created_at, lang)}</p>
        <p className="course-page__set-card-meta">{formatDifficultySummaryFn(setItem)}</p>
        {Array.isArray(setItem.source_document_names) && setItem.source_document_names.length > 0 ? (
          <p className="course-page__set-card-meta course-page__set-card-meta--sources">
            {setItem.source_document_names.join(', ')}
          </p>
        ) : null}
        <button
          type="button"
          className="course-page__start-attempt-btn"
          onClick={onStartAttempt}
          disabled={startDisabled}
        >
          {isStarting ? labels.questionSetLoading : labels.startAttempt}
        </button>
      </div>
    </article>
  )
}

function QuestionReviewList({
  questions,
  practiceAnswers,
  questionMode,
  quizLanguage,
  labels,
  onAnswerSelect,
  isSubmittingAttempt,
  onCancelAttempt,
  onSubmitQuiz,
  onBack,
  backLabel,
}) {
  const isPractice = questionMode === 'practice'
  const shouldReveal = questionMode === 'results'

  return (
    <>
      <ol className="course-page__question-list">
        {questions.map((question, index) => {
          const qid = String(question.question_id ?? question.questionId ?? `q-${index}`)
          const selectedIndex = practiceAnswers[qid]
          return (
            <li key={qid} className="course-page__question-card">
              <p className="course-page__question-title">{question.question}</p>
              <ul className="course-page__question-options">
                {(Array.isArray(question.options) ? question.options : []).map((opt, optIndex) => {
                  const correctIndex = Number(question.correct_index)
                  const isCorrectOption = optIndex === correctIndex
                  const isIncorrectSelection =
                    shouldReveal &&
                    selectedIndex !== undefined &&
                    selectedIndex === optIndex &&
                    selectedIndex !== correctIndex
                  const isSelectedInPractice = isPractice && selectedIndex === optIndex
                  const optionClassName = [
                    'course-page__question-option',
                    shouldReveal && isCorrectOption ? 'course-page__question-option--correct' : '',
                    isIncorrectSelection ? 'course-page__question-option--incorrect' : '',
                    isSelectedInPractice ? 'course-page__question-option--selected' : '',
                  ]
                    .filter(Boolean)
                    .join(' ')
                  return (
                    <li key={`${qid}-${optIndex}`}>
                      <button
                        type="button"
                        className={optionClassName}
                        onClick={() => {
                          if (!isPractice || !onAnswerSelect) return
                          onAnswerSelect(qid, optIndex)
                        }}
                        disabled={!isPractice}
                      >
                        {getOptionLabel(optIndex, quizLanguage)}. {opt}
                      </button>
                    </li>
                  )
                })}
              </ul>
              {shouldReveal ? (
                <>
                  <p className="course-page__question-answer">
                    {labels.correctAnswer}: {(question.options || [])[Number(question.correct_index)]}
                  </p>
                  <p className="course-page__question-explanation">
                    {labels.aiExplanation}: {question.explanation}
                  </p>
                </>
              ) : null}
            </li>
          )
        })}
      </ol>
      {isPractice && questions.length > 0 ? (
        <div className="course-page__quiz-actions">
          <button
            type="button"
            className="course-page__cancel-attempt-btn"
            onClick={onCancelAttempt}
            disabled={isSubmittingAttempt}
          >
            {labels.cancelAttempt}
          </button>
          <button
            type="button"
            className="course-page__submit-quiz-btn"
            onClick={onSubmitQuiz}
            disabled={isSubmittingAttempt || Object.keys(practiceAnswers).length === 0}
          >
            {isSubmittingAttempt ? labels.submitQuizSubmitting : labels.submitQuiz}
          </button>
        </div>
      ) : null}
      {shouldReveal && onBack && backLabel ? (
        <button type="button" className="course-page__back-to-sets-btn" onClick={onBack}>
          {backLabel}
        </button>
      ) : null}
    </>
  )
}

export default function CoursePage() {
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const { courseId: courseIdParam } = useParams()
  const { t, lang, setLang, dir, tx } = useLanguageControl()
  const [authStatus, setAuthStatus] = useState('loading')
  const [activeTab, setActiveTab] = useState(() => {
    const tab = searchParams.get('tab')
    if (tab === 'questionSets') return 'questionSets'
    if (tab === 'attempts') return 'attempts'
    if (tab === 'weaknesses') return 'weaknesses'
    return 'materials'
  })
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
  const [isQuizGenerateModalOpen, setIsQuizGenerateModalOpen] = useState(false)
  const [quizGenerateQuestionCount, setQuizGenerateQuestionCount] = useState(5)
  const [quizGenerateLanguage, setQuizGenerateLanguage] = useState('he')
  const [quizGenerateFocusWeakTopics, setQuizGenerateFocusWeakTopics] = useState(false)
  const [quizModalQuestionCount, setQuizModalQuestionCount] = useState(5)
  const [quizModalLanguage, setQuizModalLanguage] = useState('he')
  const [quizModalFocusWeakTopics, setQuizModalFocusWeakTopics] = useState(false)
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
  const [questionMode, setQuestionMode] = useState(null)
  const [practiceAnswers, setPracticeAnswers] = useState({})
  const [practiceStartTime, setPracticeStartTime] = useState(null)
  const [isSubmittingAttempt, setIsSubmittingAttempt] = useState(false)
  const [setPendingDelete, setSetPendingDelete] = useState(null)
  const [isDeletingSet, setIsDeletingSet] = useState(false)
  const [questionSetsNotice, setQuestionSetsNotice] = useState(null)
  const [lastSubmittedScore, setLastSubmittedScore] = useState(null)
  const [startingSetId, setStartingSetId] = useState(null)
  const [attempts, setAttempts] = useState([])
  const [isAttemptsLoading, setIsAttemptsLoading] = useState(false)
  const [attemptsError, setAttemptsError] = useState(null)
  const [viewingPastAttempt, setViewingPastAttempt] = useState(null)
  const [loadingAttemptId, setLoadingAttemptId] = useState(null)
  const [attemptPendingDelete, setAttemptPendingDelete] = useState(null)
  const [isDeletingAttempt, setIsDeletingAttempt] = useState(false)
  const [progressMatrix, setProgressMatrix] = useState({})
  const [progressTopics, setProgressTopics] = useState(null)
  const [progressLoading, setProgressLoading] = useState(false)
  const [progressError, setProgressError] = useState(null)
  const fileInputRef = useRef(null)
  const dragDepthRef = useRef(0)
  const skipSetAutoLoadRef = useRef(false)

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

  const loadDocuments = useCallback(async ({ silent = false } = {}) => {
    if (!courseId) return null
    if (!silent) {
      setDocumentsLoading(true)
      setDocumentsError(null)
      setDocumentsNotice(null)
    }
    try {
      const session = await fetchAuthSession()
      const idToken = session.tokens?.idToken?.toString()
      if (!idToken) {
        if (!silent) {
          setDocuments([])
          setDocumentsError(t.coursePage.uploadMissingSession)
        }
        return null
      }
      const list = await getCourseDocuments(courseId, idToken)
      setDocuments(list)
      return list
    } catch (err) {
      if (silent) {
        setDocumentsNotice(t.coursePage.documentsRefreshFailed)
        return null
      }
      let message = t.coursePage.documentsError
      const apiMsg = err?.response?.data?.message
      if (typeof apiMsg === 'string' && apiMsg.trim()) {
        message = apiMsg.trim()
      } else if (typeof err?.message === 'string' && err.message.includes('VITE_API_URL')) {
        message = t.coursePage.uploadApiNotConfigured
      }
      setDocumentsError(message)
      setDocuments([])
      return null
    } finally {
      if (!silent) {
        setDocumentsLoading(false)
      }
    }
  }, [courseId, t])

  const loadQuestionSets = useCallback(async ({ silent = false } = {}) => {
    if (!courseId) return null
    if (!silent) {
      setQuestionSetsLoading(true)
      setQuestionSetsError(null)
    }
    try {
      const session = await fetchAuthSession()
      const idToken = session.tokens?.idToken?.toString()
      if (!idToken) {
        if (!silent) {
          setQuestionSets([])
          setQuestionSetsError(t.coursePage.uploadMissingSession)
        }
        return null
      }
      const sets = await getQuestionSets(courseId, idToken)
      setQuestionSets(sets)
      return sets
    } catch (err) {
      if (silent) {
        return null
      }
      const apiMsg = err?.response?.data?.message
      setQuestionSetsError(
        typeof apiMsg === 'string' && apiMsg.trim()
          ? apiMsg.trim()
          : t.coursePage.questionSetsLoadError,
      )
      setQuestionSets([])
      return null
    } finally {
      if (!silent) {
        setQuestionSetsLoading(false)
      }
    }
  }, [courseId, t])

  const loadQuestionSetDetails = useCallback(
    async (setId, { startPractice = false } = {}) => {
      if (!courseId || !setId) return false
      setSetQuestionsLoading(true)
      setSetQuestionsError(null)
      try {
        const session = await fetchAuthSession()
        const idToken = session.tokens?.idToken?.toString()
        if (!idToken) {
          setSetQuestions([])
          setSetQuestionsError(t.coursePage.uploadMissingSession)
          return false
        }
        const payload = await getQuestionSetDetails(courseId, setId, idToken)
        const questions = Array.isArray(payload?.questions) ? payload.questions : []
        setSelectedQuestionSet(payload?.set ?? null)
        setSetQuestions(questions)
        setPracticeAnswers({})
        if (startPractice) {
          if (questions.length === 0) {
            setQuestionMode(null)
            setPracticeStartTime(null)
            return false
          }
          setQuestionMode('practice')
          setPracticeStartTime(Date.now())
        } else {
          setQuestionMode(null)
          setPracticeStartTime(null)
        }
        return true
      } catch (err) {
        const apiMsg = err?.response?.data?.message
        setSetQuestionsError(
          typeof apiMsg === 'string' && apiMsg.trim()
            ? apiMsg.trim()
            : t.coursePage.questionSetLoadError,
        )
        return false
      } finally {
        setSetQuestionsLoading(false)
      }
    },
    [courseId, t],
  )

  const loadCourseAttempts = useCallback(async () => {
    if (!courseId) return
    setIsAttemptsLoading(true)
    setAttemptsError(null)
    try {
      const session = await fetchAuthSession()
      const idToken = session.tokens?.idToken?.toString()
      if (!idToken) {
        setAttempts([])
        setAttemptsError(t.coursePage.uploadMissingSession)
        return
      }
      const rows = await getCourseAttempts(courseId, idToken)
      setAttempts(rows)
    } catch (err) {
      const apiMsg = err?.response?.data?.message
      setAttemptsError(
        typeof apiMsg === 'string' && apiMsg.trim()
          ? apiMsg.trim()
          : t.coursePage.attemptsLoadError,
      )
      setAttempts([])
    } finally {
      setIsAttemptsLoading(false)
    }
  }, [courseId, t])

  const loadCourseProgress = useCallback(async () => {
    if (!courseId) return
    setProgressLoading(true)
    setProgressError(null)
    try {
      const session = await fetchAuthSession()
      const idToken = session.tokens?.idToken?.toString()
      if (!idToken) {
        setProgressMatrix({})
        setProgressTopics(null)
        setProgressError(t.coursePage.uploadMissingSession)
        return
      }
      const payload = await getCourseProgress(courseId, idToken)
      setProgressMatrix(payload?.matrix && typeof payload.matrix === 'object' ? payload.matrix : {})
      setProgressTopics(Array.isArray(payload?.topics) ? payload.topics : null)
    } catch (err) {
      const apiMsg = err?.response?.data?.message
      setProgressError(
        typeof apiMsg === 'string' && apiMsg.trim()
          ? apiMsg.trim()
          : t.coursePage.weaknessesLoadError,
      )
      setProgressMatrix({})
      setProgressTopics(null)
    } finally {
      setProgressLoading(false)
    }
  }, [courseId, t])

  useEffect(() => {
    if (authStatus !== 'authed' || !courseId) return
    loadDocuments({ silent: activeTab !== 'materials' })
    // Prefetch once per course/auth; silent when landing on a non-materials tab.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authStatus, courseId, loadDocuments])

  useEffect(() => {
    if (authStatus !== 'authed' || !courseId || activeTab !== 'questionSets') return
    loadQuestionSets()
  }, [authStatus, courseId, activeTab, loadQuestionSets])

  useEffect(() => {
    if (skipSetAutoLoadRef.current) return
    if (authStatus !== 'authed' || activeTab !== 'questionSets' || !selectedQuestionSet?.set_id) return
    if (questionMode === 'practice' || questionMode === 'results') return
    loadQuestionSetDetails(selectedQuestionSet.set_id)
  }, [
    authStatus,
    activeTab,
    selectedQuestionSet?.set_id,
    questionMode,
    loadQuestionSetDetails,
  ])

  useEffect(() => {
    if (authStatus !== 'authed' || !courseId || activeTab !== 'attempts') return
    loadCourseAttempts()
    if (questionSets.length === 0) {
      loadQuestionSets()
    }
  }, [authStatus, courseId, activeTab, loadCourseAttempts, loadQuestionSets, questionSets.length])

  useEffect(() => {
    if (authStatus !== 'authed' || !courseId || activeTab !== 'weaknesses') return
    loadCourseProgress()
  }, [authStatus, courseId, activeTab, loadCourseProgress])

  useEffect(() => {
    if (activeTab !== 'attempts') return
    setSelectedQuestionSet(null)
    if (!viewingPastAttempt) {
      setSetQuestions([])
      setSetQuestionsError(null)
    }
  }, [activeTab, viewingPastAttempt])

  const handleExitPastAttemptView = useCallback(() => {
    setViewingPastAttempt(null)
    setLoadingAttemptId(null)
    setSelectedQuestionSet(null)
    setPracticeAnswers({})
    setSetQuestions([])
    setQuestionMode(null)
    setSetQuestionsError(null)
    setSetQuestionsLoading(false)
  }, [])

  useEffect(() => {
    if (activeTab === 'attempts') return
    if (!viewingPastAttempt) return
    handleExitPastAttemptView()
  }, [activeTab, viewingPastAttempt, handleExitPastAttemptView])

  useEffect(() => {
    if (activeTab === 'questionSets') return

    setQuestionSetsNotice(null)
    setLastSubmittedScore(null)

    if (questionMode !== 'results') return

    // Past-attempt review on the attempts tab also uses questionMode === 'results'.
    if (activeTab === 'attempts' && viewingPastAttempt) return

    setSelectedQuestionSet(null)
    setSetQuestions([])
    setQuestionMode(null)
    setPracticeAnswers({})
    setPracticeStartTime(null)
  }, [activeTab, questionMode, viewingPastAttempt])

  const loadPastAttemptReview = useCallback(
    async (attempt) => {
      const attemptId = String(attempt?.attempt_id ?? '').trim()
      const setId = String(attempt?.question_set_id ?? '').trim()
      if (!courseId || !attemptId || !setId) return

      setViewingPastAttempt(attempt)
      setSetQuestionsLoading(true)
      setSetQuestionsError(null)
      setLoadingAttemptId(attemptId)
      try {
        const session = await fetchAuthSession()
        const idToken = session.tokens?.idToken?.toString()
        if (!idToken) {
          setSetQuestionsError(t.coursePage.uploadMissingSession)
          return
        }

        const [setPayload, answers] = await Promise.all([
          getQuestionSetDetails(courseId, setId, idToken),
          getAttemptAnswers(courseId, attemptId, idToken),
        ])
        const questions = Array.isArray(setPayload?.questions) ? setPayload.questions : []
        setSelectedQuestionSet(setPayload?.set ?? null)
        setSetQuestions(questions)
        setPracticeAnswers(mapAttemptAnswersToPractice(questions, answers))
        setQuestionMode('results')
      } catch (err) {
        const apiMsg = err?.response?.data?.message
        setSetQuestionsError(
          typeof apiMsg === 'string' && apiMsg.trim()
            ? apiMsg.trim()
            : t.coursePage.pastAttemptLoadError,
        )
        setQuestionMode(null)
      } finally {
        setSetQuestionsLoading(false)
        setLoadingAttemptId(null)
      }
    },
    [courseId, t],
  )

  const handleViewPastAttempt = useCallback(
    (attempt) => {
      if (setQuestionsLoading || loadingAttemptId) return
      const attemptId = String(attempt?.attempt_id ?? '').trim()
      const setId = String(attempt?.question_set_id ?? '').trim()
      if (!attemptId || !setId) return
      loadPastAttemptReview(attempt)
    },
    [setQuestionsLoading, loadingAttemptId, loadPastAttemptReview],
  )

  const handleStartAttemptFromSet = useCallback(
    async (setItem) => {
      const setId = setItem?.set_id
      if (!courseId || !setId || startingSetId) return

      setStartingSetId(setId)
      setSetQuestionsError(null)
      setQuestionSetsNotice(null)
      setLastSubmittedScore(null)
      setSelectedQuestionSet(setItem)

      skipSetAutoLoadRef.current = true
      const started = await loadQuestionSetDetails(setId, { startPractice: true })
      skipSetAutoLoadRef.current = false

      if (!started) {
        setSelectedQuestionSet(null)
        setSetQuestions([])
      }
      setStartingSetId(null)
    },
    [courseId, startingSetId, loadQuestionSetDetails],
  )

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

  const shouldPollDocuments = documents.some((doc) => {
    const status = normalizeProcessingStatus(doc.processing_status ?? doc.processingStatus)
    return !FINAL_PROCESSING_STATUSES.has(status)
  })

  useEffect(() => {
    if (authStatus !== 'authed' || !courseId || !shouldPollDocuments) return undefined
    if (isGeneratingQuiz) return undefined
    if (activeTab !== 'materials') return undefined

    const intervalId = window.setInterval(() => {
      loadDocuments({ silent: true })
    }, DOCUMENT_POLL_INTERVAL_MS)

    return () => window.clearInterval(intervalId)
  }, [authStatus, courseId, shouldPollDocuments, isGeneratingQuiz, activeTab, loadDocuments])

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
      await loadDocuments({ silent: documents.length > 0 })
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
  }, [courseId, isUploading, loadDocuments, selectedFiles, documents.length, t, tx])

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
    } else if (tabParam === 'attempts' && activeTab !== 'attempts') {
      setActiveTab('attempts')
    } else if (tabParam === 'weaknesses' && activeTab !== 'weaknesses') {
      setActiveTab('weaknesses')
    } else if (!tabParam && activeTab !== 'materials') {
      setActiveTab('materials')
    }
    if (!setParam && selectedQuestionSet?.set_id && questionMode !== 'practice' && questionMode !== 'results') {
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
    } else if (activeTab === 'attempts') {
      nextParams.set('tab', 'attempts')
      nextParams.delete('set')
    } else if (activeTab === 'weaknesses') {
      nextParams.set('tab', 'weaknesses')
      nextParams.delete('set')
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

  const closeQuizGenerateModal = useCallback(() => {
    if (isGeneratingQuiz) return
    setIsQuizGenerateModalOpen(false)
  }, [isGeneratingQuiz])

  useEffect(() => {
    if (!isQuizGenerateModalOpen) return undefined
    const onKeyDown = (e) => {
      if (e.key === 'Escape' && !isGeneratingQuiz) closeQuizGenerateModal()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [isQuizGenerateModalOpen, isGeneratingQuiz, closeQuizGenerateModal])

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

  const topicCatalog = useMemo(() => buildTopicCatalog(documents), [documents])

  const resolvedWeaknessRows = useMemo(() => {
    const topics = resolveProgressTopics({ matrix: progressMatrix, topics: progressTopics })
    return topics.map((topicData) => {
      const englishKey = topicData.topic
      const displayLabel = resolveMatrixTopicLabel(englishKey, topicCatalog, lang)
      const breakdown = topicData.difficulty_breakdown || {}
      const cells = WEAKNESS_DIFFICULTIES.map((diff) => {
        const key = diff.toLowerCase()
        const cell = breakdown[key] ?? {}
        const total = Number(cell.total) || 0
        const correct = Number(cell.correct) || 0
        const pct = cell.score != null ? cell.score : masteryPercent(correct, total)
        return { difficulty: diff, correct, total, pct, tierClass: getMasteryTierClass(pct) }
      })
      return {
        englishKey,
        displayLabel,
        score: topicData.score,
        status: topicData.status,
        cells,
      }
    })
  }, [progressMatrix, progressTopics, topicCatalog, lang])

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
  const showDocSkeleton = documentsLoading && documents.length === 0
  const showDocList = !showDocSkeleton && !documentsError && documents.length > 0
  const showDocEmpty = !showDocSkeleton && !documentsLoading && !documentsError && documents.length === 0
  const showQuestionSetsSkeleton = questionSetsLoading && questionSets.length === 0
  const inQuizSession = questionMode === 'practice' || questionMode === 'results'
  const quizContentDir =
    inQuizSession || viewingPastAttempt
      ? resolveQuizContentDir(selectedQuestionSet)
      : undefined
  const showQuestionSetDetail = Boolean(
    selectedQuestionSet && (inQuizSession || setQuestionsLoading),
  )
  const showWeaknessesSkeleton =
    (progressLoading && resolvedWeaknessRows.length === 0) ||
    (documentsLoading && topicCatalog.size === 0 && resolvedWeaknessRows.length === 0)

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

  const handleOpenQuizGenerateModal = () => {
    if (selectedDocIds.length === 0 || isGeneratingQuiz) return
    setQuizModalQuestionCount(quizGenerateQuestionCount)
    setQuizModalLanguage(quizGenerateLanguage)
    setQuizModalFocusWeakTopics(quizGenerateFocusWeakTopics)
    setIsQuizGenerateModalOpen(true)
  }

  const handleConfirmGenerateQuiz = async () => {
    if (selectedDocIds.length === 0 || isGeneratingQuiz) return
    const requestedQuestionCount = quizModalQuestionCount
    const quizLanguage = quizModalLanguage
    const focusWeakTopics = quizModalFocusWeakTopics
    setQuizGenerateQuestionCount(requestedQuestionCount)
    setQuizGenerateLanguage(quizLanguage)
    setQuizGenerateFocusWeakTopics(focusWeakTopics)
    setIsQuizGenerateModalOpen(false)
    await handleGenerateQuiz({ requestedQuestionCount, quizLanguage, focusWeakTopics })
  }

  const handleGenerateQuiz = async (options = {}) => {
    if (!courseId || selectedDocIds.length === 0 || isGeneratingQuiz) return

    const requestedQuestionCount =
      options.requestedQuestionCount ?? quizGenerateQuestionCount
    const quizLanguage = options.quizLanguage ?? quizGenerateLanguage
    const focusWeakTopics = options.focusWeakTopics ?? quizGenerateFocusWeakTopics
    const pendingDocIds = [...selectedDocIds]
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
      await generateQuiz(courseId, pendingDocIds, idToken, {
        requestedQuestionCount,
        quizLanguage,
        focusWeakTopics,
      })

      const result = await waitForQuizCompletion(
        pendingDocIds,
        loadDocuments,
        loadQuestionSets,
        t.coursePage,
      )
      if (!result.ok) {
        throw new Error(result.message || t.coursePage.quizGenerationFailed)
      }

      setSelectionMode(false)
      setSelectedDocIds([])
      setQuestionSetsNotice(t.coursePage.quizGenerationComplete)
      setQuizStartedNotice(null)
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

  const handleCloseQuestionSet = () => {
    setSelectedQuestionSet(null)
    setSetQuestions([])
    setQuestionMode(null)
    setPracticeAnswers({})
    setPracticeStartTime(null)
    setSetQuestionsError(null)
    setQuestionSetsNotice(null)
    setLastSubmittedScore(null)
  }

  const handleCancelAttempt = () => {
    handleCloseQuestionSet()
  }

  const handleSubmitQuiz = async () => {
    const setId = selectedQuestionSet?.set_id
    if (!courseId || !setId || isSubmittingAttempt) return

    setIsSubmittingAttempt(true)
    setSetQuestionsError(null)
    try {
      const session = await fetchAuthSession()
      const idToken = session.tokens?.idToken?.toString()
      if (!idToken) {
        setSetQuestionsError(t.coursePage.uploadMissingSession)
        return
      }

      const startedAt = practiceStartTime ?? Date.now()
      const timeSpentSeconds = Math.floor((Date.now() - startedAt) / 1000)
      const result = await submitAttempt(
        courseId,
        setId,
        {
          time_spent_seconds: timeSpentSeconds,
          answers: practiceAnswers,
        },
        idToken,
      )

      const score = result?.score
      const displayScore = score !== undefined && score !== null ? score : '—'
      setLastSubmittedScore(displayScore)
      setQuestionSetsNotice(
        tx(t.coursePage.submitQuizSuccess, {
          score: displayScore,
        }),
      )
      setQuestionMode('results')
      setPracticeStartTime(null)
      await loadCourseAttempts()
    } catch (err) {
      const apiMsg = err?.response?.data?.message
      if (typeof apiMsg === 'string' && apiMsg.trim()) {
        setSetQuestionsError(apiMsg.trim())
      } else if (typeof err?.message === 'string' && err.message.includes('VITE_API_URL')) {
        setSetQuestionsError(t.coursePage.uploadApiNotConfigured)
      } else {
        setSetQuestionsError(t.coursePage.submitQuizError)
      }
    } finally {
      setIsSubmittingAttempt(false)
    }
  }

  const handleDeleteAttempt = async () => {
    const attemptId = String(attemptPendingDelete?.attempt_id ?? '').trim()
    if (!attemptId || !courseId || isDeletingAttempt) return
    setIsDeletingAttempt(true)
    setAttemptsError(null)
    try {
      const session = await fetchAuthSession()
      const idToken = session.tokens?.idToken?.toString()
      if (!idToken) throw new Error(t.coursePage.uploadMissingSession)
      await deleteAttempt(courseId, attemptId, idToken)
      setAttempts((prev) => prev.filter((row) => String(row.attempt_id ?? '').trim() !== attemptId))
      if (String(viewingPastAttempt?.attempt_id ?? '').trim() === attemptId) {
        handleExitPastAttemptView()
      }
      setAttemptPendingDelete(null)
      await loadCourseProgress()
    } catch (err) {
      const apiMsg = err?.response?.data?.message
      setAttemptsError(
        typeof apiMsg === 'string' && apiMsg.trim() ? apiMsg.trim() : t.coursePage.deleteAttemptError,
      )
    } finally {
      setIsDeletingAttempt(false)
    }
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
            <button
              type="button"
              className={`course-page__inner-nav-item ${
                activeTab === 'attempts' ? 'course-page__inner-nav-item--active' : ''
              }`}
              onClick={() => setActiveTab('attempts')}
              aria-current={activeTab === 'attempts' ? 'page' : undefined}
            >
              {t.coursePage.tabAttempts}
            </button>
            <button
              type="button"
              className={`course-page__inner-nav-item ${
                activeTab === 'weaknesses' ? 'course-page__inner-nav-item--active' : ''
              }`}
              onClick={() => setActiveTab('weaknesses')}
              aria-current={activeTab === 'weaknesses' ? 'page' : undefined}
            >
              {t.coursePage.tabWeaknesses}
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

              {showDocSkeleton ? (
                <div className="course-page__documents-skeleton" aria-busy="true">
                  <div className="course-page__documents-skeleton-row" />
                  <div className="course-page__documents-skeleton-row" />
                  <div className="course-page__documents-skeleton-row" />
                  <p className="course-page__documents-state">{t.coursePage.documentsLoading}</p>
                </div>
              ) : null}

              {documentsError && !showDocSkeleton ? (
                <p className="course-page__documents-error" role="alert">
                  {documentsError}
                </p>
              ) : null}

              {quizError && !showDocSkeleton ? (
                <div className="course-page__quiz-error" role="alert">
                  <p className="course-page__quiz-error-text">{quizError}</p>
                  <button
                    type="button"
                    className="course-page__quiz-error-retry"
                    onClick={handleOpenQuizGenerateModal}
                    disabled={isGeneratingQuiz || selectedDocIds.length === 0}
                  >
                    {t.coursePage.tryAgain}
                  </button>
                </div>
              ) : null}

              {documentsNotice && !showDocSkeleton ? (
                <p className="course-page__documents-notice" role="status">
                  {documentsNotice}
                </p>
              ) : null}

              {quizStartedNotice && !showDocSkeleton ? (
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
                    const hasGeneratedQuiz = Boolean(
                      doc.has_generated_quiz ?? doc.hasGeneratedQuiz ?? false,
                    )
                    const statusLabel = documentStatusLabel(status, t.coursePage)
                    const showStatusBadge =
                      normalizedStatus && normalizedStatus !== 'READY'
                    const deletingThisDoc = deletingDocId === String(id)
                    const docId = String(id)
                    const isSelected = selectedDocIds.includes(docId)
                    const topics = normalizeTopics(doc)
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
                          {topics.length > 0 ? (
                            <ul
                              className="course-page__doc-topics"
                              aria-label={t.coursePage.documentTopicsAria}
                            >
                              {topics.map((topic) => {
                                const topicKey = `${String(id)}-${topic.en || topic.he}`
                                return (
                                  <li key={topicKey}>
                                    <span className="course-page__topic-chip">
                                      {getTopicLabel(topic, lang)}
                                    </span>
                                  </li>
                                )
                              })}
                            </ul>
                          ) : null}
                        </div>
                        {!isInteractive ? <span className="mini-spinner" aria-hidden /> : null}
                        {showStatusBadge ? (
                          <span
                            className={`course-page__doc-badge${
                              normalizedStatus === 'FAILED' || normalizedStatus === 'ERROR'
                                ? ' course-page__doc-badge--failed'
                                : ''
                            }`}
                            role="status"
                          >
                            {statusLabel}
                          </span>
                        ) : null}
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
                <p
                  className={`course-page__score-notice course-page__score-notice--${
                    getScoreTier(lastSubmittedScore) ?? 'high'
                  }`}
                  role="status"
                >
                  {questionSetsNotice}
                </p>
              ) : null}
              {!showQuestionSetDetail ? (
                <>
                  {showQuestionSetsSkeleton ? (
                    <div className="course-page__documents-skeleton" aria-busy="true">
                      <div className="course-page__documents-skeleton-row" />
                      <div className="course-page__documents-skeleton-row" />
                    </div>
                  ) : null}
                  {!showQuestionSetsSkeleton && questionSets.length === 0 ? (
                    <p className="course-page__materials-empty">{t.coursePage.questionSetsEmpty}</p>
                  ) : null}
                  {!showQuestionSetsSkeleton && questionSets.length > 0 ? (
                    <ul className="course-page__set-list course-page__set-list--preview">
                      {questionSets.map((setItem) => {
                        const setId = setItem.set_id
                        const questionCount = Number(
                          setItem.question_count ?? setItem.questionCount ?? 0,
                        )
                        return (
                          <li key={setId} className="course-page__set-list-item">
                            <QuestionSetPreviewCard
                              setItem={setItem}
                              labels={t.coursePage}
                              lang={lang}
                              txFn={tx}
                              formatDocumentDateFn={formatDocumentDate}
                              formatDifficultySummaryFn={formatDifficultySummary}
                              isStarting={startingSetId === setId}
                              startDisabled={Boolean(startingSetId) || questionCount === 0}
                              onStartAttempt={() => handleStartAttemptFromSet(setItem)}
                              onDelete={() => setSetPendingDelete(setItem)}
                              deleteAriaLabel={tx(t.coursePage.deleteSetAria, {
                                name:
                                  setItem.name ||
                                  setItem.set_name ||
                                  t.coursePage.questionSetUntitled,
                              })}
                            />
                          </li>
                        )
                      })}
                    </ul>
                  ) : null}
                </>
              ) : (
                <div className="course-page__set-detail" dir={quizContentDir}>
                  <div className="course-page__set-detail-top">
                    <button
                      type="button"
                      className="course-page__back-btn"
                      onClick={handleCloseQuestionSet}
                    >
                      {t.coursePage.backToSets}
                    </button>
                  </div>
                  {setQuestionsLoading ? (
                    <p className="course-page__documents-state">{t.coursePage.questionSetLoading}</p>
                  ) : null}
                  {setQuestionsError ? (
                    <p className="course-page__documents-error" role="alert">
                      {setQuestionsError}
                    </p>
                  ) : null}
                  {!setQuestionsLoading &&
                  (questionMode === 'practice' || questionMode === 'results') ? (
                    <QuestionReviewList
                      questions={setQuestions}
                      practiceAnswers={practiceAnswers}
                      questionMode={questionMode}
                      quizLanguage={resolveQuizLanguage(selectedQuestionSet)}
                      labels={t.coursePage}
                      onAnswerSelect={(qid, optIndex) => {
                        setPracticeAnswers((prev) => ({
                          ...prev,
                          [qid]: optIndex,
                        }))
                      }}
                      isSubmittingAttempt={isSubmittingAttempt}
                      onCancelAttempt={handleCancelAttempt}
                      onSubmitQuiz={handleSubmitQuiz}
                      onBack={questionMode === 'results' ? handleCloseQuestionSet : undefined}
                      backLabel={questionMode === 'results' ? t.coursePage.backToSets : undefined}
                    />
                  ) : null}
                </div>
              )}
            </section>
          ) : null}
          {activeTab === 'attempts' ? (
            <section aria-label={t.coursePage.attemptsSectionLabel}>
              <div className="course-page__materials-header">
                <h2 className="course-page__materials-heading course-page__materials-heading--panel">
                  {t.coursePage.attemptsHeading}
                </h2>
              </div>
              {viewingPastAttempt ? (
                <div className="course-page__past-attempt-detail" dir={quizContentDir}>
                  <div className="course-page__set-detail-top">
                    <button
                      type="button"
                      className="course-page__back-btn"
                      onClick={handleExitPastAttemptView}
                      disabled={setQuestionsLoading}
                    >
                      {t.coursePage.backToAttemptsHistory}
                    </button>
                  </div>
                  <p className="course-page__past-attempt-header">
                    {tx(t.coursePage.viewingPastAttemptHeader, {
                      date: formatDocumentDate(viewingPastAttempt.submitted_at, lang),
                      score: viewingPastAttempt.score ?? '—',
                      setName: resolveQuestionSetLabel(
                        viewingPastAttempt.question_set_id,
                        questionSets,
                      ),
                    })}
                  </p>
                  {setQuestionsLoading ? (
                    <p className="course-page__documents-state">{t.coursePage.questionSetLoading}</p>
                  ) : null}
                  {setQuestionsError ? (
                    <p className="course-page__documents-error" role="alert">
                      {setQuestionsError}
                    </p>
                  ) : null}
                  {!setQuestionsLoading &&
                  !setQuestionsError &&
                  questionMode === 'results' &&
                  setQuestions.length > 0 ? (
                    <QuestionReviewList
                      questions={setQuestions}
                      practiceAnswers={practiceAnswers}
                      questionMode="results"
                      quizLanguage={resolveQuizLanguage(selectedQuestionSet)}
                      labels={t.coursePage}
                      onBack={handleExitPastAttemptView}
                      backLabel={t.coursePage.backToAttemptsHistory}
                    />
                  ) : null}
                </div>
              ) : (
                <>
                  {isAttemptsLoading ? (
                    <div className="course-page__documents-skeleton" aria-busy="true">
                      <div className="course-page__documents-skeleton-row" />
                      <div className="course-page__documents-skeleton-row" />
                      <p className="course-page__documents-state">{t.coursePage.attemptsLoading}</p>
                    </div>
                  ) : null}
                  {attemptsError && !isAttemptsLoading ? (
                    <p className="course-page__documents-error" role="alert">
                      {attemptsError}
                    </p>
                  ) : null}
                  {!isAttemptsLoading && !attemptsError && attempts.length === 0 ? (
                    <p className="course-page__materials-empty">{t.coursePage.attemptsEmpty}</p>
                  ) : null}
                  {!isAttemptsLoading && !attemptsError && attempts.length > 0 ? (
                    <ul
                      className="course-page__attempt-list"
                      aria-label={t.coursePage.attemptsListAriaLabel}
                    >
                      {attempts.map((attempt) => {
                        const attemptKey =
                          attempt.attempt_id ?? `${attempt.submitted_at}-${attempt.question_set_id}`
                        const attemptId = String(attempt.attempt_id ?? '').trim()
                        const isLoadingThis = loadingAttemptId === attemptId
                        const attemptDate = formatDocumentDate(attempt.submitted_at, lang)
                        const attemptScore = attempt.score ?? '—'
                        return (
                          <li key={attemptKey}>
                            <article className="course-page__attempt-card">
                              <div className="course-page__attempt-preview-toolbar">
                                <button
                                  type="button"
                                  className="course-page__set-delete-btn course-page__set-delete-btn--inline"
                                  onClick={() => setAttemptPendingDelete(attempt)}
                                  disabled={
                                    isDeletingAttempt ||
                                    setQuestionsLoading ||
                                    Boolean(loadingAttemptId)
                                  }
                                  aria-label={tx(t.coursePage.deleteAttemptAria, {
                                    date: attemptDate,
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
                              </div>
                              <button
                                type="button"
                                className="course-page__attempt-card--clickable"
                                onClick={() => handleViewPastAttempt(attempt)}
                                disabled={setQuestionsLoading || Boolean(loadingAttemptId)}
                                aria-label={tx(t.coursePage.viewPastAttemptAria, {
                                  date: attemptDate,
                                  score: attemptScore,
                                })}
                              >
                                <div className="course-page__attempt-card-body">
                                  <p className="course-page__attempt-card-date">{attemptDate}</p>
                                  <p className="course-page__attempt-card-meta">
                                    {tx(t.coursePage.attemptScoreLabel, {
                                      score: attemptScore,
                                    })}
                                  </p>
                                  <p className="course-page__attempt-card-meta">
                                    {tx(t.coursePage.attemptTimeSpentLabel, {
                                      time: formatTimeSpent(
                                        attempt.time_spent_seconds,
                                        t.coursePage,
                                        tx,
                                      ),
                                    })}
                                  </p>
                                  <p className="course-page__attempt-card-meta">
                                    {tx(t.coursePage.attemptQuestionSetLabel, {
                                      name: resolveQuestionSetLabel(
                                        attempt.question_set_id,
                                        questionSets,
                                      ),
                                    })}
                                  </p>
                                  {isLoadingThis ? (
                                    <p className="course-page__documents-state">
                                      {t.coursePage.questionSetLoading}
                                    </p>
                                  ) : null}
                                </div>
                              </button>
                            </article>
                          </li>
                        )
                      })}
                    </ul>
                  ) : null}
                </>
              )}
            </section>
          ) : null}
          {activeTab === 'weaknesses' ? (
            <section aria-label={t.coursePage.weaknessesSectionLabel}>
              <div className="course-page__materials-header">
                <h2 className="course-page__materials-heading course-page__materials-heading--panel">
                  {t.coursePage.weaknessesHeading}
                </h2>
              </div>

              {showWeaknessesSkeleton ? (
                <div className="course-page__documents-skeleton" aria-busy="true">
                  <div className="course-page__documents-skeleton-row" />
                  <div className="course-page__documents-skeleton-row" />
                  <p className="course-page__documents-state">{t.coursePage.weaknessesLoading}</p>
                </div>
              ) : null}

              {progressError && !showWeaknessesSkeleton ? (
                <p className="course-page__documents-error" role="alert">
                  {progressError}
                </p>
              ) : null}

              {!showWeaknessesSkeleton &&
              !progressLoading &&
              !progressError &&
              resolvedWeaknessRows.length === 0 ? (
                <div className="course-page__weakness-empty">
                  <svg
                    className="course-page__weakness-empty-icon"
                    width="80"
                    height="64"
                    viewBox="0 0 80 64"
                    fill="none"
                    aria-hidden="true"
                  >
                    <rect
                      x="8"
                      y="12"
                      width="64"
                      height="40"
                      rx="6"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      opacity="0.35"
                    />
                    <path
                      d="M20 40h12M36 32h8M52 36h12"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      opacity="0.5"
                    />
                  </svg>
                  <h3 className="course-page__weakness-empty-title">
                    {t.coursePage.weaknessesEmptyTitle}
                  </h3>
                  <p className="course-page__weakness-empty-body">
                    {t.coursePage.weaknessesEmptyBody}
                  </p>
                  <button
                    type="button"
                    className="course-page__upload-btn"
                    onClick={() => setActiveTab('questionSets')}
                  >
                    {t.coursePage.weaknessesEmptyCta}
                  </button>
                </div>
              ) : null}

              {!showWeaknessesSkeleton &&
              !progressError &&
              resolvedWeaknessRows.length > 0 ? (
                <>
                  <TopicScoreChart
                    topics={resolvedWeaknessRows}
                    labels={t.coursePage}
                    txFn={tx}
                  />
                  <ul
                    className="course-page__weakness-list"
                    aria-label={t.coursePage.weaknessListAriaLabel}
                  >
                    {resolvedWeaknessRows.map((row) => {
                      const statusLabelKey = WEAKNESS_STATUS_LABEL_KEYS[row.status]
                      const statusLabel = statusLabelKey
                        ? t.coursePage[statusLabelKey]
                        : row.status
                      const statusChipClass =
                        WEAKNESS_STATUS_CHIP_CLASS[row.status] ??
                        WEAKNESS_STATUS_CHIP_CLASS.weak
                      return (
                        <li key={row.englishKey}>
                          <article className="course-page__weakness-card">
                            <div className="course-page__weakness-card-header">
                              <h3 className="course-page__weakness-card-title">{row.displayLabel}</h3>
                              <div className="course-page__weakness-card-meta">
                                <span className="course-page__weakness-card-score">
                                  {tx(t.coursePage.weaknessTopicScoreLabel, { score: row.score })}
                                </span>
                                <span
                                  className={`course-page__weakness-status-chip ${statusChipClass}`}
                                >
                                  {statusLabel}
                                </span>
                              </div>
                            </div>
                            <div className="course-page__weakness-rows">
                              {row.cells.map((cell) => {
                                const diffLabelKey = WEAKNESS_DIFFICULTY_LABEL_KEYS[cell.difficulty]
                                const diffLabel = diffLabelKey
                                  ? t.coursePage[diffLabelKey]
                                  : cell.difficulty
                                const practiced = cell.total > 0
                                const statsText = practiced
                                  ? tx(t.coursePage.weaknessStatsPracticed, {
                                      correct: cell.correct,
                                      total: cell.total,
                                    })
                                  : '—'
                                const masteryText = practiced
                                  ? tx(t.coursePage.weaknessMasteryLabel, { percent: cell.pct })
                                  : t.coursePage.weaknessNotYetPracticed
                                return (
                                  <div
                                    key={`${row.englishKey}-${cell.difficulty}`}
                                    className="course-page__weakness-row"
                                  >
                                    <span className="course-page__weakness-difficulty">
                                      {diffLabel}
                                    </span>
                                    <span className="course-page__weakness-stats">{statsText}</span>
                                    <div className="course-page__weakness-mastery">
                                      <span className="course-page__weakness-mastery-text">
                                        {masteryText}
                                      </span>
                                      <div
                                        className="course-page__weakness-bar-track"
                                        role="progressbar"
                                        aria-valuemin={0}
                                        aria-valuemax={100}
                                        {...(practiced
                                          ? { 'aria-valuenow': cell.pct }
                                          : {
                                              'aria-valuetext':
                                                t.coursePage.weaknessNotYetPracticed,
                                            })}
                                      >
                                        <span
                                          className={`course-page__mastery-fill ${cell.tierClass}`}
                                          style={{ width: practiced ? `${cell.pct}%` : '0%' }}
                                        />
                                      </div>
                                    </div>
                                  </div>
                                )
                              })}
                            </div>
                          </article>
                        </li>
                      )
                    })}
                  </ul>
                </>
              ) : null}
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
      {attemptPendingDelete ? (
        <div
          className="course-page__modal-backdrop"
          role="presentation"
          onClick={() => {
            if (!isDeletingAttempt) setAttemptPendingDelete(null)
          }}
        >
          <section
            className="course-page__modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="course-delete-attempt-modal-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 id="course-delete-attempt-modal-title" className="course-page__modal-title">
              {t.coursePage.deleteAttempt}
            </h2>
            <p className="course-page__modal-subtitle">
              {tx(t.coursePage.deleteAttemptConfirm, {
                date: formatDocumentDate(attemptPendingDelete.submitted_at, lang),
                score: attemptPendingDelete.score ?? '—',
              })}
            </p>
            <div className="course-page__modal-actions">
              <button
                type="button"
                className="course-page__modal-cancel"
                onClick={() => setAttemptPendingDelete(null)}
                disabled={isDeletingAttempt}
              >
                {t.home.cancel}
              </button>
              <button
                type="button"
                className="course-page__modal-submit course-page__modal-submit--danger"
                onClick={handleDeleteAttempt}
                disabled={isDeletingAttempt}
              >
                {isDeletingAttempt ? t.coursePage.deleteDeleting : t.coursePage.deleteAttempt}
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
            onClick={handleOpenQuizGenerateModal}
            disabled={isGeneratingQuiz || selectedDocIds.length === 0}
          >
            {t.coursePage.quizGenerate}
          </button>
        </div>
      ) : null}
      {isQuizGenerateModalOpen ? (
        <div
          className="course-page__modal-backdrop"
          role="presentation"
          onClick={closeQuizGenerateModal}
        >
          <section
            className="course-page__modal course-page__modal--quiz-generate"
            role="dialog"
            aria-modal="true"
            aria-labelledby="course-quiz-generate-modal-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 id="course-quiz-generate-modal-title" className="course-page__modal-title">
              {t.coursePage.quizGenerateModalTitle}
            </h2>
            <p className="course-page__modal-subtitle">{t.coursePage.quizGenerateModalSubtitle}</p>

            <fieldset className="course-page__quiz-generate-field">
              <legend className="course-page__quiz-generate-legend">
                {t.coursePage.quizGenerateCountLabel}
              </legend>
              <div
                className="course-page__quiz-generate-options"
                role="radiogroup"
                aria-label={t.coursePage.quizGenerateCountAria}
              >
                {QUIZ_GENERATE_COUNT_OPTIONS.map((count) => (
                  <label key={count} className="course-page__quiz-generate-option">
                    <input
                      type="radio"
                      name="quiz-generate-count"
                      value={count}
                      checked={quizModalQuestionCount === count}
                      onChange={() => setQuizModalQuestionCount(count)}
                      disabled={isGeneratingQuiz}
                    />
                    <span>{count}</span>
                  </label>
                ))}
              </div>
            </fieldset>

            <fieldset className="course-page__quiz-generate-field">
              <legend className="course-page__quiz-generate-legend">
                {t.coursePage.quizGenerateLanguageLabel}
              </legend>
              <div
                className="course-page__quiz-generate-options"
                role="radiogroup"
                aria-label={t.coursePage.quizGenerateLanguageAria}
              >
                <label className="course-page__quiz-generate-option">
                  <input
                    type="radio"
                    name="quiz-generate-language"
                    value="he"
                    checked={quizModalLanguage === 'he'}
                    onChange={() => setQuizModalLanguage('he')}
                    disabled={isGeneratingQuiz}
                  />
                  <span>{t.coursePage.quizLanguageOptionHe}</span>
                </label>
                <label className="course-page__quiz-generate-option">
                  <input
                    type="radio"
                    name="quiz-generate-language"
                    value="en"
                    checked={quizModalLanguage === 'en'}
                    onChange={() => setQuizModalLanguage('en')}
                    disabled={isGeneratingQuiz}
                  />
                  <span>{t.coursePage.quizLanguageOptionEn}</span>
                </label>
              </div>
            </fieldset>

            <div className="course-page__quiz-generate-focus">
              <label className="course-page__quiz-generate-focus-label">
                <input
                  type="checkbox"
                  checked={quizModalFocusWeakTopics}
                  onChange={(e) => setQuizModalFocusWeakTopics(e.target.checked)}
                  disabled={isGeneratingQuiz}
                  aria-label={t.coursePage.quizGenerateFocusWeakAria}
                  aria-describedby="course-quiz-generate-focus-helper"
                />
                <span>{t.coursePage.quizGenerateFocusWeakLabel}</span>
              </label>
              <p
                id="course-quiz-generate-focus-helper"
                className="course-page__quiz-generate-focus-helper"
              >
                {t.coursePage.quizGenerateFocusWeakHelper}
              </p>
            </div>

            <div className="course-page__modal-actions">
              <button
                type="button"
                className="course-page__modal-cancel"
                onClick={closeQuizGenerateModal}
                disabled={isGeneratingQuiz}
              >
                {t.home.cancel}
              </button>
              <button
                type="button"
                className="course-page__modal-submit"
                onClick={handleConfirmGenerateQuiz}
                disabled={
                  isGeneratingQuiz || selectedDocIds.length === 0
                }
              >
                {t.coursePage.quizGenerate}
              </button>
            </div>
          </section>
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
