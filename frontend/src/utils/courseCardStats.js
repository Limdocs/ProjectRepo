import { resolveProgressTopics } from './topicScoring.js'

const DOCUMENT_TIMESTAMP_KEYS = ['created_at', 'createdAt', 'updated_at', 'uploaded_at']

export function parseIsoMs(value) {
  if (!value || typeof value !== 'string') return null
  const ms = new Date(value).getTime()
  return Number.isNaN(ms) ? null : ms
}

export function maxIsoTimestamp(...candidates) {
  let best = null
  let bestMs = null
  for (const c of candidates) {
    if (!c || typeof c !== 'string') continue
    const ms = parseIsoMs(c)
    if (ms == null) continue
    if (bestMs == null || ms > bestMs) {
      bestMs = ms
      best = c
    }
  }
  return best
}

function documentTimestampCandidates(documents) {
  const out = []
  if (!Array.isArray(documents)) return out
  for (const doc of documents) {
    if (!doc || typeof doc !== 'object') continue
    for (const key of DOCUMENT_TIMESTAMP_KEYS) {
      const v = doc[key]
      if (v && typeof v === 'string') out.push(v)
    }
  }
  return out
}

export function pickLastUpdatedIso(course, documents) {
  const fromCourse = [course?.updated_at, course?.updatedAt].filter(
    (v) => v && typeof v === 'string',
  )
  const fromDocs = documentTimestampCandidates(documents)
  const best = maxIsoTimestamp(...fromCourse, ...fromDocs)
  if (best) return best
  return maxIsoTimestamp(course?.created_at, course?.createdAt)
}

export function computeCourseProgressPercent(progressPayload) {
  const topics = resolveProgressTopics(progressPayload)
  if (!topics.length) return 0
  const avg = topics.reduce((s, t) => s + (Number(t.score) || 0), 0) / topics.length
  return Math.min(100, Math.max(0, Math.round(avg)))
}

export function buildCourseCardStats(course, documents, progressPayload) {
  const docs = Array.isArray(documents) ? documents : []
  return {
    documentCount: docs.length,
    lastUpdatedIso: pickLastUpdatedIso(course, docs),
    progressPercent: computeCourseProgressPercent(progressPayload),
  }
}
