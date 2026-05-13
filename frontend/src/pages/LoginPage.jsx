import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  confirmSignUp,
  fetchAuthSession,
  getCurrentUser,
  signIn,
  signUp,
} from 'aws-amplify/auth'
import './LoginPage.css'
import { useLanguageControl } from '../language-control/LanguageControlProvider.jsx'

function logAuthError(context, error) {
  const message = error?.message ?? String(error)
  const name = error?.name ?? error?.code
  console.warn('[Auth i18n draft]', context, { name, message, error })
}

const initialLogin = { identifier: '', password: '' }
const initialSignup = {
  firstName: '',
  lastName: '',
  email: '',
  username: '',
  password: '',
}

const apiBaseUrl = import.meta.env.VITE_API_URL ?? ''
const USER_SYNC_PENDING_KEY = 'limdocs:userSyncPending'
const USER_SYNC_DONE_PREFIX = 'limdocs:userSyncDone:'

function Feedback({ feedback }) {
  if (!feedback) return null
  const className =
    feedback.kind === 'success'
      ? 'login-page__feedback login-page__feedback--success'
      : 'login-page__feedback login-page__feedback--error'
  return (
    <p className={className} role="alert">
      {feedback.message}
    </p>
  )
}

export default function LoginPage({ initialMode = 'login' }) {
  const navigate = useNavigate()
  const { t, lang, setLang, dir } = useLanguageControl()
  const [mode, setMode] = useState(initialMode)
  const [login, setLogin] = useState(initialLogin)
  const [loginErrors, setLoginErrors] = useState({})
  const [showConfirmLink, setShowConfirmLink] = useState(false)
  const [signup, setSignup] = useState(initialSignup)
  const [fieldErrors, setFieldErrors] = useState({})
  const [confirmCode, setConfirmCode] = useState('')
  const [pendingUsername, setPendingUsername] = useState('')
  const [busy, setBusy] = useState(false)
  const [feedback, setFeedback] = useState(null)
  const signupRequirementMessages = t.login.requirements

  const setPendingUserSync = (payload) => {
    sessionStorage.setItem(USER_SYNC_PENDING_KEY, JSON.stringify(payload))
  }

  const getPendingUserSync = () => {
    const raw = sessionStorage.getItem(USER_SYNC_PENDING_KEY)
    if (!raw) return null
    try {
      return JSON.parse(raw)
    } catch {
      return null
    }
  }

  const clearPendingUserSync = () => {
    sessionStorage.removeItem(USER_SYNC_PENDING_KEY)
  }

  const isUserSynced = (sub) =>
    !!localStorage.getItem(`${USER_SYNC_DONE_PREFIX}${sub}`)

  const markUserSynced = (sub) => {
    localStorage.setItem(`${USER_SYNC_DONE_PREFIX}${sub}`, '1')
  }

  const syncUserAfterRegistration = async () => {
    if (!apiBaseUrl) return
    const pending = getPendingUserSync()
    if (!pending) return

    const session = await fetchAuthSession()
    const idToken = session?.tokens?.idToken?.toString()
    const claims = session?.tokens?.idToken?.payload ?? {}
    const sub = claims?.sub
    if (!idToken || !sub) return

    if (isUserSynced(sub)) {
      clearPendingUserSync()
      return
    }

    const email = claims.email ?? pending.email ?? ''
    const username = claims['cognito:username'] ?? pending.username ?? ''
    const first_name = claims.given_name ?? pending.first_name ?? ''
    const last_name = claims.family_name ?? pending.last_name ?? ''

    if (!email || !username || !first_name || !last_name) {
      throw new Error('Missing required user fields for /users sync')
    }

    const response = await fetch(`${apiBaseUrl}/users`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${idToken}`,
      },
      body: JSON.stringify({
        email,
        username,
        first_name,
        last_name,
      }),
    })

    if (!response.ok) {
      const text = await response.text()
      throw new Error(`User sync failed (${response.status}): ${text}`)
    }

    markUserSynced(sub)
    clearPendingUserSync()
  }

  useEffect(() => {
    setFeedback(null)
    setLoginErrors({})
    setFieldErrors({})
    setShowConfirmLink(false)
  }, [lang])

  useEffect(() => {
    if (initialMode !== 'login' && initialMode !== 'signup') return
    setMode(initialMode)
    setFeedback(null)
    setLoginErrors({})
    setFieldErrors({})
    setShowConfirmLink(false)
  }, [initialMode])

  useEffect(() => {
    if (mode !== 'login' && mode !== 'signup') return
    let cancelled = false
    ;(async () => {
      try {
        await getCurrentUser()
        await syncUserAfterRegistration().catch((error) => {
          logAuthError('syncUserAfterRegistration on session restore', error)
        })
        if (!cancelled) navigate('/home', { replace: true })
      } catch {
        /* no session */
      }
    })()
    return () => {
      cancelled = true
    }
  }, [mode, navigate])

  const goLogin = () => {
    setFeedback(null)
    setLoginErrors({})
    setShowConfirmLink(false)
    navigate('/login')
  }

  const goSignup = () => {
    setFeedback(null)
    setLoginErrors({})
    setFieldErrors({})
    setShowConfirmLink(false)
    navigate('/register')
  }

  const updateLogin = (field, value) => {
    setLogin((prev) => ({ ...prev, [field]: value }))
    setShowConfirmLink(false)
    setLoginErrors((prev) => {
      if (!prev[field]) return prev
      const next = { ...prev }
      delete next[field]
      return next
    })
  }

  const updateSignup = (field, value) => {
    setSignup((prev) => ({ ...prev, [field]: value }))
    setFieldErrors((prev) => {
      if (!prev[field]) return prev
      const next = { ...prev }
      delete next[field]
      return next
    })
  }

  const handleLoginSubmit = async (e) => {
    e.preventDefault()
    setFeedback(null)
    setLoginErrors({})
    setShowConfirmLink(false)
    const username = login.identifier.trim()
    const nextLoginErrors = {}
    if (!username) {
      nextLoginErrors.identifier = t.login.requiredIdentifier
    }
    if (!login.password) {
      nextLoginErrors.password = t.login.requiredPassword
    }
    if (Object.keys(nextLoginErrors).length > 0) {
      setLoginErrors(nextLoginErrors)
      setFeedback({
        kind: 'error',
        message: t.login.fillMarkedFields,
      })
      return
    }
    setBusy(true)
    try {
      const out = await signIn({ username, password: login.password })
      const signInDone =
        out.isSignedIn || out.nextStep?.signInStep === 'DONE'
      if (signInDone) {
        await syncUserAfterRegistration().catch((error) => {
          logAuthError('syncUserAfterRegistration after signIn', error)
        })
        navigate('/home', { replace: true })
        return
      }
      logAuthError('signIn nextStep', out.nextStep)
      setFeedback({
        kind: 'error',
        message: t.login.signInNextStep,
      })
    } catch (err) {
      if (err?.name === 'UserAlreadyAuthenticatedException') {
        await syncUserAfterRegistration().catch((error) => {
          logAuthError(
            'syncUserAfterRegistration after already-authenticated signIn',
            error,
          )
        })
        navigate('/home', { replace: true })
        return
      }
      logAuthError('signIn', err)
      const errorName = err?.name ?? err?.code
      if (errorName === 'UserNotFoundException') {
        setShowConfirmLink(false)
        setLoginErrors({
          identifier: t.login.userNotFound,
        })
      } else if (errorName === 'NotAuthorizedException') {
        setShowConfirmLink(false)
        setLoginErrors({
          password: t.login.wrongPassword,
        })
      } else if (errorName === 'UserNotConfirmedException') {
        setShowConfirmLink(true)
        setLoginErrors({
          identifier: t.login.userNotConfirmed,
        })
      } else {
        setShowConfirmLink(false)
        setFeedback({
          kind: 'error',
          message: t.login.signInFailed,
        })
      }
    } finally {
      setBusy(false)
    }
  }

  const handleSignupSubmit = async (e) => {
    e.preventDefault()
    setFeedback(null)
    setFieldErrors({})
    const username = signup.username.trim()
    const email = signup.email.trim()
    const firstName = signup.firstName.trim()
    const lastName = signup.lastName.trim()
    const password = signup.password

    const nextFieldErrors = {}
    const fullName = `${firstName} ${lastName}`.trim()
    const fullNameWords = fullName.split(/\s+/).filter(Boolean)
    const hasValidFullName = fullNameWords.length >= 2
    const hasValidEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)
    const hasValidUsername = username.length >= 3 && !/\s/.test(username)
    const hasValidPassword =
      password.length >= 8 &&
      /[A-Z]/.test(password) &&
      /[a-z]/.test(password) &&
      /\d/.test(password) &&
      /[^A-Za-z0-9]/.test(password)

    if (!hasValidFullName) {
      nextFieldErrors.firstName = signupRequirementMessages.fullName
      nextFieldErrors.lastName = signupRequirementMessages.fullName
    }
    if (!hasValidEmail) {
      nextFieldErrors.email = signupRequirementMessages.email
    }
    if (!hasValidUsername) {
      nextFieldErrors.username = signupRequirementMessages.username
    }
    if (!hasValidPassword) {
      nextFieldErrors.password = signupRequirementMessages.password
    }

    if (Object.keys(nextFieldErrors).length > 0) {
      setFieldErrors(nextFieldErrors)
      setFeedback({ kind: 'error', message: t.login.fixMarkedFields })
      return
    }
    setBusy(true)
    try {
      const out = await signUp({
        username,
        password,
        options: {
          userAttributes: {
            email,
            given_name: firstName,
            family_name: lastName,
          },
        },
      })
      const step = out.nextStep?.signUpStep
      if (step === 'CONFIRM_SIGN_UP') {
        setPendingUserSync({
          email,
          username,
          first_name: firstName,
          last_name: lastName,
        })
        setPendingUsername(username)
        setConfirmCode('')
        setMode('confirm')
        setFeedback({
          kind: 'success',
          message: t.login.signUpCodeSent,
        })
        return
      }
      if (out.isSignUpComplete) {
        setPendingUserSync({
          email,
          username,
          first_name: firstName,
          last_name: lastName,
        })
        setFeedback({
          kind: 'success',
          message: t.login.signUpComplete,
        })
        setMode('login')
        return
      }
      logAuthError('signUp unexpected nextStep', out)
      setFeedback({
        kind: 'error',
        message: t.login.signUpNotComplete,
      })
    } catch (err) {
      logAuthError('signUp', err)
      const errorName = err?.name ?? err?.code
      if (errorName === 'UsernameExistsException') {
        setFieldErrors({ username: signupRequirementMessages.username })
      } else if (errorName === 'InvalidParameterException') {
        setFieldErrors({ email: signupRequirementMessages.email })
      } else if (errorName === 'InvalidPasswordException') {
        setFieldErrors({
          password: signupRequirementMessages.password,
        })
      }
      setFeedback({ kind: 'error', message: t.login.signUpFailed })
    } finally {
      setBusy(false)
    }
  }

  const handleConfirmSubmit = async (e) => {
    e.preventDefault()
    setFeedback(null)
    const code = confirmCode.trim()
    if (!pendingUsername || !code) {
      setFeedback({ kind: 'error', message: t.login.confirmCodeRequired })
      return
    }
    setBusy(true)
    try {
      await confirmSignUp({
        username: pendingUsername,
        confirmationCode: code,
      })
      setConfirmCode('')
      setPendingUsername('')
      setFeedback({
        kind: 'success',
        message: t.login.confirmSuccess,
      })
      setMode('login')
    } catch (err) {
      logAuthError('confirmSignUp', err)
      setFeedback({
        kind: 'error',
        message: t.login.confirmFailed,
      })
    } finally {
      setBusy(false)
    }
  }

  const handleForgotPassword = () => {
    console.log('[forgot-password]', { identifier: login.identifier })
  }

  return (
    <div className="login-page" dir={dir} lang={lang}>
      <div className="login-page__inner">
        <div className="login-page__lang-switch" role="group" aria-label={t.common.switchLanguage}>
          <button
            type="button"
            className={`login-page__lang-btn ${lang === 'he' ? 'login-page__lang-btn--active' : ''}`}
            onClick={() => setLang('he')}
          >
            {t.common.langHe}
          </button>
          <button
            type="button"
            className={`login-page__lang-btn ${lang === 'en' ? 'login-page__lang-btn--active' : ''}`}
            onClick={() => setLang('en')}
          >
            {t.common.langEn}
          </button>
        </div>
        <header className="login-page__brand">
          <div className="login-page__logo" aria-hidden>
            L
          </div>
          <h1 className="login-page__title">{t.login.brandTitle}</h1>
          <p className="login-page__subtitle">{t.login.brandSubtitle}</p>
        </header>

        <div className="login-page__card">
          <Feedback feedback={feedback} />

          {mode === 'login' ? (
            <form
              aria-label={t.login.loginFormLabel}
              onSubmit={handleLoginSubmit}
              noValidate
            >
              <div className="login-page__field">
                <label className="login-page__label" htmlFor="login-identifier">
                  {t.login.identifierLabel}
                </label>
                <input
                  id="login-identifier"
                  className={`login-page__input ${loginErrors.identifier ? 'login-page__input--error' : ''}`}
                  type="text"
                  name="identifier"
                  autoComplete="username"
                  value={login.identifier}
                  onChange={(e) =>
                    updateLogin('identifier', e.target.value)
                  }
                />
                {loginErrors.identifier ? (
                  <span className="field-error">{loginErrors.identifier}</span>
                ) : null}
                {showConfirmLink && loginErrors.identifier ? (
                  <button
                    type="button"
                    className="login-page__link"
                    onClick={() => {
                      setPendingUsername(login.identifier.trim())
                      setMode('confirm')
                    }}
                  >
                    {t.login.goToConfirm}
                  </button>
                ) : null}
              </div>
              <div className="login-page__field">
                <label className="login-page__label" htmlFor="login-password">
                  {t.login.passwordLabel}
                </label>
                <input
                  id="login-password"
                  className={`login-page__input ${loginErrors.password ? 'login-page__input--error' : ''}`}
                  type="password"
                  name="password"
                  autoComplete="current-password"
                  value={login.password}
                  onChange={(e) => updateLogin('password', e.target.value)}
                />
                {loginErrors.password ? (
                  <span className="field-error">{loginErrors.password}</span>
                ) : null}
              </div>
              <button
                type="submit"
                className="login-page__submit"
                disabled={busy}
              >
                {t.login.loginButton}
              </button>
              <div className="login-page__links">
                <button
                  type="button"
                  className="login-page__link"
                  onClick={handleForgotPassword}
                >
                  {t.login.forgotPassword}
                </button>
                <button
                  type="button"
                  className="login-page__link"
                  onClick={goSignup}
                >
                  {t.login.createNewAccount}
                </button>
              </div>
            </form>
          ) : mode === 'signup' ? (
            <form
              aria-label={t.login.signupFormLabel}
              onSubmit={handleSignupSubmit}
              noValidate
            >
              <div className="login-page__field">
                <label className="login-page__label" htmlFor="signup-first-name">
                  {t.login.firstNameLabel}
                </label>
                <input
                  id="signup-first-name"
                  className={`login-page__input ${fieldErrors.firstName ? 'login-page__input--error' : ''}`}
                  type="text"
                  name="firstName"
                  autoComplete="given-name"
                  value={signup.firstName}
                  onChange={(e) => updateSignup('firstName', e.target.value)}
                />
                {fieldErrors.firstName ? (
                  <span className="field-error">{fieldErrors.firstName}</span>
                ) : null}
              </div>
              <div className="login-page__field">
                <label className="login-page__label" htmlFor="signup-last-name">
                  {t.login.lastNameLabel}
                </label>
                <input
                  id="signup-last-name"
                  className={`login-page__input ${fieldErrors.lastName ? 'login-page__input--error' : ''}`}
                  type="text"
                  name="lastName"
                  autoComplete="family-name"
                  value={signup.lastName}
                  onChange={(e) => updateSignup('lastName', e.target.value)}
                />
                {fieldErrors.lastName ? (
                  <span className="field-error">{fieldErrors.lastName}</span>
                ) : null}
              </div>
              <div className="login-page__field">
                <label className="login-page__label" htmlFor="signup-email">
                  {t.login.emailLabel}
                </label>
                <input
                  id="signup-email"
                  className={`login-page__input ${fieldErrors.email ? 'login-page__input--error' : ''}`}
                  type="email"
                  name="email"
                  autoComplete="email"
                  value={signup.email}
                  onChange={(e) => updateSignup('email', e.target.value)}
                />
                {fieldErrors.email ? (
                  <span className="field-error">{fieldErrors.email}</span>
                ) : null}
              </div>
              <div className="login-page__field">
                <label className="login-page__label" htmlFor="signup-username">
                  {t.login.usernameLabel}
                </label>
                <input
                  id="signup-username"
                  className={`login-page__input ${fieldErrors.username ? 'login-page__input--error' : ''}`}
                  type="text"
                  name="username"
                  autoComplete="username"
                  value={signup.username}
                  onChange={(e) => updateSignup('username', e.target.value)}
                />
                {fieldErrors.username ? (
                  <span className="field-error">{fieldErrors.username}</span>
                ) : null}
              </div>
              <div className="login-page__field">
                <label className="login-page__label" htmlFor="signup-password">
                  {t.login.passwordLabel}
                </label>
                <input
                  id="signup-password"
                  className={`login-page__input ${fieldErrors.password ? 'login-page__input--error' : ''}`}
                  type="password"
                  name="password"
                  autoComplete="new-password"
                  value={signup.password}
                  onChange={(e) => updateSignup('password', e.target.value)}
                />
                {fieldErrors.password ? (
                  <span className="field-error">{fieldErrors.password}</span>
                ) : null}
              </div>
              <button
                type="submit"
                className="login-page__submit"
                disabled={busy}
              >
                {t.login.signupButton}
              </button>
              <div className="login-page__links">
                <button
                  type="button"
                  className="login-page__link"
                  onClick={goLogin}
                >
                  {t.login.alreadyHaveAccount}
                </button>
              </div>
            </form>
          ) : (
            <form
              aria-label={t.login.confirmFormLabel}
              onSubmit={handleConfirmSubmit}
              noValidate
            >
              <p className="login-page__label login-page__label--spaced">
                {t.login.confirmPrompt}{' '}
                <strong dir="ltr"> {signup.email.trim() || t.login.yourEmail}</strong>
              </p>
              <div className="login-page__field">
                <label className="login-page__label" htmlFor="confirm-code">
                  {t.login.confirmCodeLabel}
                </label>
                <input
                  id="confirm-code"
                  className="login-page__input"
                  type="text"
                  name="confirmationCode"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  value={confirmCode}
                  onChange={(e) => setConfirmCode(e.target.value)}
                />
              </div>
              <button
                type="submit"
                className="login-page__submit"
                disabled={busy}
              >
                {t.login.confirmButton}
              </button>
              <div className="login-page__links">
                <button
                  type="button"
                  className="login-page__link"
                  onClick={() => {
                    setPendingUsername('')
                    setConfirmCode('')
                    goLogin()
                  }}
                >
                  {t.login.backToLogin}
                </button>
              </div>
            </form>
          )}
        </div>

        <p className="login-page__footer">{t.login.footer}</p>
      </div>
    </div>
  )
}
