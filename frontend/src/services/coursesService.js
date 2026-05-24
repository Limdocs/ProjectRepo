import axios from 'axios'

const apiBaseUrl = import.meta.env.VITE_API_URL ?? ''

export async function getUserCourses(userId, idToken) {
  if (!apiBaseUrl) {
    throw new Error('API is not configured. Set VITE_API_URL.')
  }
  if (!userId) {
    throw new Error('Missing userId.')
  }
  if (!idToken) {
    throw new Error('Missing idToken.')
  }

  const response = await axios.get(`${apiBaseUrl}/users/${encodeURIComponent(userId)}/courses`, {
    headers: {
      Authorization: `Bearer ${idToken}`,
    },
  })

  const payload = response?.data
  if (Array.isArray(payload)) {
    return payload
  }
  if (Array.isArray(payload?.courses)) {
    return payload.courses
  }
  return []
}

export async function deleteCourse(courseId, idToken) {
  if (!apiBaseUrl) {
    throw new Error('API is not configured. Set VITE_API_URL.')
  }
  if (!courseId) {
    throw new Error('Missing courseId.')
  }
  if (!idToken) {
    throw new Error('Missing idToken.')
  }

  const response = await axios.delete(`${apiBaseUrl}/courses/${encodeURIComponent(courseId)}`, {
    headers: {
      Authorization: `Bearer ${idToken}`,
    },
  })

  return response?.data ?? {}
}

export async function submitAttempt(courseId, setId, payload, idToken) {
  if (!apiBaseUrl) {
    throw new Error('API is not configured. Set VITE_API_URL.')
  }
  if (!courseId) {
    throw new Error('Missing courseId.')
  }
  if (!setId) {
    throw new Error('Missing setId.')
  }
  if (!idToken) {
    throw new Error('Missing idToken.')
  }

  const response = await axios.post(
    `${apiBaseUrl}/courses/${encodeURIComponent(courseId)}/question-sets/${encodeURIComponent(setId)}/attempts`,
    payload,
    {
      headers: {
        Authorization: `Bearer ${idToken}`,
      },
    },
  )

  return response?.data ?? {}
}

export async function getCourseAttempts(courseId, idToken) {
  if (!apiBaseUrl) {
    throw new Error('API is not configured. Set VITE_API_URL.')
  }
  if (!courseId) {
    throw new Error('Missing courseId.')
  }
  if (!idToken) {
    throw new Error('Missing idToken.')
  }

  const response = await axios.get(
    `${apiBaseUrl}/courses/${encodeURIComponent(courseId)}/attempts`,
    {
      headers: {
        Authorization: `Bearer ${idToken}`,
      },
    },
  )

  const payload = response?.data
  if (Array.isArray(payload)) {
    return payload
  }
  if (Array.isArray(payload?.attempts)) {
    return payload.attempts
  }
  return []
}

export async function getAttemptAnswers(courseId, attemptId, idToken) {
  if (!apiBaseUrl) {
    throw new Error('API is not configured. Set VITE_API_URL.')
  }
  if (!courseId) {
    throw new Error('Missing courseId.')
  }
  if (!attemptId) {
    throw new Error('Missing attemptId.')
  }
  if (!idToken) {
    throw new Error('Missing idToken.')
  }

  const response = await axios.get(
    `${apiBaseUrl}/courses/${encodeURIComponent(courseId)}/attempts/${encodeURIComponent(attemptId)}/answers`,
    {
      headers: {
        Authorization: `Bearer ${idToken}`,
      },
    },
  )

  const payload = response?.data
  if (payload?.answers && typeof payload.answers === 'object') {
    return payload.answers
  }
  return {}
}

export async function deleteAttempt(courseId, attemptId, idToken) {
  if (!apiBaseUrl) {
    throw new Error('API is not configured. Set VITE_API_URL.')
  }
  if (!courseId) {
    throw new Error('Missing courseId.')
  }
  if (!attemptId) {
    throw new Error('Missing attemptId.')
  }
  if (!idToken) {
    throw new Error('Missing idToken.')
  }

  const response = await axios.delete(
    `${apiBaseUrl}/courses/${encodeURIComponent(courseId)}/attempts/${encodeURIComponent(attemptId)}`,
    {
      headers: {
        Authorization: `Bearer ${idToken}`,
      },
    },
  )

  return response?.data ?? {}
}
