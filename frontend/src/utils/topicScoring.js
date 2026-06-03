const DIFFICULTY_WEIGHTS = { Easy: 1, Medium: 2, Hard: 3 }

const TOPIC_STATUS_THRESHOLDS = {
  weak_max: 59,
  medium_max: 79,
}

const BREAKDOWN_KEYS = { Easy: 'easy', Medium: 'medium', Hard: 'hard' }
const LEGACY_TOPIC_ALIASES = { General: 'Uncategorized' }

function safeInt(value, defaultValue = 0) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return defaultValue
  return Math.trunc(parsed)
}

function normalizeTopicKey(topic) {
  return LEGACY_TOPIC_ALIASES[topic] ?? topic
}

function difficultyWeight(difficulty) {
  return DIFFICULTY_WEIGHTS[difficulty] ?? 2
}

function topicStatus(score) {
  if (score <= TOPIC_STATUS_THRESHOLDS.weak_max) return 'weak'
  if (score <= TOPIC_STATUS_THRESHOLDS.medium_max) return 'medium'
  return 'strong'
}

function mergeMatrix(matrix) {
  const merged = {}
  if (!matrix || typeof matrix !== 'object') return merged

  for (const [topicKey, difficulties] of Object.entries(matrix)) {
    if (!difficulties || typeof difficulties !== 'object') continue
    const canonical = normalizeTopicKey(topicKey)
    if (!merged[canonical]) merged[canonical] = {}

    for (const [difficulty, cell] of Object.entries(difficulties)) {
      if (!cell || typeof cell !== 'object') continue
      if (!merged[canonical][difficulty]) {
        merged[canonical][difficulty] = { correct: 0, total: 0 }
      }
      merged[canonical][difficulty].correct += safeInt(cell.correct)
      merged[canonical][difficulty].total += safeInt(cell.total)
    }
  }
  return merged
}

export function computeTopicScores(matrix) {
  const merged = mergeMatrix(matrix)
  const topics = []

  for (const [topic, difficulties] of Object.entries(merged)) {
    let weightedCorrect = 0
    let weightedTotal = 0
    let correctCount = 0
    let totalAnswered = 0
    const difficultyBreakdown = {
      easy: { correct: 0, total: 0, score: null },
      medium: { correct: 0, total: 0, score: null },
      hard: { correct: 0, total: 0, score: null },
    }

    for (const [difficulty, cell] of Object.entries(difficulties)) {
      if (!cell || typeof cell !== 'object') continue
      const correctD = safeInt(cell.correct)
      const totalD = safeInt(cell.total)
      const weight = difficultyWeight(difficulty)
      weightedCorrect += weight * correctD
      weightedTotal += weight * totalD
      correctCount += correctD
      totalAnswered += totalD

      const breakdownKey = BREAKDOWN_KEYS[difficulty]
      if (breakdownKey) {
        difficultyBreakdown[breakdownKey].correct += correctD
        difficultyBreakdown[breakdownKey].total += totalD
      }
    }

    for (const breakdown of Object.values(difficultyBreakdown)) {
      if (breakdown.total > 0) {
        breakdown.score = Math.round((breakdown.correct / breakdown.total) * 100)
      }
    }

    if (weightedTotal === 0) continue

    const score = Math.round((weightedCorrect / weightedTotal) * 100)
    topics.push({
      topic,
      score,
      status: topicStatus(score),
      total_answered: totalAnswered,
      correct_count: correctCount,
      wrong_count: totalAnswered - correctCount,
      weighted_correct: weightedCorrect,
      weighted_total: weightedTotal,
      difficulty_breakdown: difficultyBreakdown,
    })
  }

  topics.sort((a, b) => a.score - b.score || a.topic.localeCompare(b.topic))
  return topics
}

export function resolveProgressTopics(payload) {
  if (Array.isArray(payload?.topics)) {
    return payload.topics
  }
  const matrix = payload?.matrix
  if (matrix && typeof matrix === 'object') {
    return computeTopicScores(matrix)
  }
  return []
}
