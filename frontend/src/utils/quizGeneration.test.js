import { describe, expect, it } from 'vitest'
import { mapGenerationFailure, resolveQuizJobStatus } from './quizGeneration.js'

const labels = {
  quizGenerationFailed: 'generic',
  quizErrorFullTextOverBudget: 'too long',
  quizErrorInsufficientPassageBudget: 'fewer documents',
  quizErrorSourcesEmpty: 'empty',
  quizErrorDocumentMissing: 'missing',
  quizErrorDispatchFailed: 'dispatch',
  quizErrorGenerationSuperseded: 'superseded',
}

describe('resolveQuizJobStatus', () => {
  it('reports running while the current generation is still generating', () => {
    expect(
      resolveQuizJobStatus({
        generation: { generation_id: 'new', generation_status: 'GENERATING' },
        questionSets: [],
        generationId: 'new',
      }).state,
    ).toBe('running')
  })

  it('ignores source document status entirely', () => {
    const documentsLookBroken = {
      generation: { generation_id: 'new', generation_status: 'GENERATING' },
      questionSets: [],
      generationId: 'new',
      documents: [{ document_id: 'doc-1', processing_status: 'FAILED' }],
      pendingDocIds: ['doc-1'],
    }
    expect(resolveQuizJobStatus(documentsLookBroken).state).toBe('running')

    expect(
      resolveQuizJobStatus({
        generation: { generation_id: 'new', generation_status: 'READY' },
        questionSets: [],
        generationId: 'new',
        documents: [{ document_id: 'doc-1', processing_status: 'FAILED' }],
      }).state,
    ).toBe('success')
  })

  it('succeeds on the current generation and never on a stale set', () => {
    expect(
      resolveQuizJobStatus({
        generation: { generation_id: 'new', generation_status: 'READY' },
        questionSets: [{ set_id: 'set-1', generation_id: 'new' }],
        generationId: 'new',
      }).state,
    ).toBe('success')

    expect(
      resolveQuizJobStatus({
        generation: { generation_id: 'new', generation_status: 'GENERATING' },
        questionSets: [{ set_id: 'old-set', generation_id: 'old' }],
        generationId: 'new',
      }).state,
    ).toBe('running')
  })

  it('accepts a committed set for this generation even without the generation read', () => {
    expect(
      resolveQuizJobStatus({
        generation: null,
        questionSets: [{ set_id: 'set-1', generation_id: 'new' }],
        generationId: 'new',
      }).state,
    ).toBe('success')
  })

  it('fails on this generation and ignores another generation row', () => {
    const failed = resolveQuizJobStatus({
      generation: {
        generation_id: 'new',
        generation_status: 'FAILED',
        failure_code: 'CONTENT_FULL_TEXT_OVER_BUDGET',
      },
      questionSets: [],
      generationId: 'new',
    })
    expect(failed.state).toBe('failed')
    expect(mapGenerationFailure(failed.failureCode, labels)).toBe('too long')

    expect(
      resolveQuizJobStatus({
        generation: {
          generation_id: 'old',
          generation_status: 'FAILED',
          failure_code: 'CONTENT_SOURCES_EMPTY',
        },
        questionSets: [],
        generationId: 'new',
      }).state,
    ).toBe('running')
  })

  it('surfaces a superseded generation', () => {
    const superseded = resolveQuizJobStatus({
      generation: {
        generation_id: 'new',
        generation_status: 'FAILED',
        failure_code: 'GENERATION_SUPERSEDED',
      },
      generationId: 'new',
    })
    expect(superseded.state).toBe('failed')
    expect(mapGenerationFailure(superseded.failureCode, labels)).toBe('superseded')
  })

  it('maps unknown codes to the generic message', () => {
    expect(mapGenerationFailure('INTERNAL_ERROR', labels)).toBe('generic')
    expect(mapGenerationFailure('CONTENT_INSUFFICIENT_PASSAGE_BUDGET', labels)).toBe(
      'fewer documents',
    )
  })

  it('does not report success without a generation id', () => {
    expect(
      resolveQuizJobStatus({
        generation: { generation_id: 'other', generation_status: 'READY' },
        questionSets: [{ generation_id: 'other' }],
        generationId: null,
      }).state,
    ).toBe('failed')
  })
})
