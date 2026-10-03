const FAILURE_LABEL_KEYS = {
  CONTENT_FULL_TEXT_OVER_BUDGET: 'quizErrorFullTextOverBudget',
  CONTENT_INSUFFICIENT_PASSAGE_BUDGET: 'quizErrorInsufficientPassageBudget',
  CONTENT_SOURCES_EMPTY: 'quizErrorSourcesEmpty',
  DOCUMENT_NOT_FOUND: 'quizErrorDocumentMissing',
  DOCUMENT_NOT_PROCESSED: 'quizErrorDocumentMissing',
  DISPATCH_FAILED: 'quizErrorDispatchFailed',
  GENERATION_SUPERSEDED: 'quizErrorGenerationSuperseded',
}

function setGenerationId(setItem) {
  return setItem?.generation_id ?? setItem?.generationId ?? null
}

function generationStatus(generation) {
  return String(generation?.generation_status ?? generation?.generationStatus ?? '')
    .trim()
    .toUpperCase()
}

export function mapGenerationFailure(code, labels) {
  const key = FAILURE_LABEL_KEYS[code]
  if (key && labels && typeof labels[key] === 'string' && labels[key].trim()) {
    return labels[key]
  }
  return labels?.quizGenerationFailed
}

/**
 * Resolve the outcome of one quiz-generation request.
 *
 * Only the generation identified by `generationId` can answer this: source
 * document statuses describe document processing health, and an older question
 * set belongs to an earlier request.
 *
 * @param {{
 *   generation?: Record<string, unknown> | null,
 *   questionSets?: Array<Record<string, unknown>>,
 *   generationId?: string | null,
 * }} input
 * @returns {{ state: 'running' | 'success' | 'failed', failureCode?: string | null }}
 */
export function resolveQuizJobStatus({ generation, questionSets, generationId }) {
  if (!generationId) {
    return { state: 'failed' }
  }

  const matchesRequest = setGenerationId(generation) === generationId
  if (matchesRequest) {
    const status = generationStatus(generation)
    if (status === 'READY') {
      return { state: 'success' }
    }
    if (status === 'FAILED') {
      const raw = generation.failure_code ?? generation.failureCode
      const failureCode = typeof raw === 'string' && raw.trim() ? raw.trim() : null
      return { state: 'failed', failureCode }
    }
  }

  const sets = Array.isArray(questionSets) ? questionSets : []
  if (sets.some((setItem) => setGenerationId(setItem) === generationId)) {
    return { state: 'success' }
  }

  return { state: 'running' }
}
