from pathlib import Path

EASE = "var(--ease-fluid)"

def ease_replace(s: str) -> str:
    for a, b in [
        ("0.22s ease", "0.22s " + EASE),
        ("0.2s ease", "0.22s " + EASE),
        ("0.18s ease", "0.18s " + EASE),
        ("0.15s ease", "0.15s " + EASE),
        ("0.3s ease", "0.28s " + EASE),
    ]:
        s = s.replace(a, b)
    return s

path = Path(r"c:\Projects\Limdocs\frontend\src\pages\CoursePage.css")
t = path.read_text(encoding="utf-8")
t = ease_replace(t)

subs = [
    ("rgba(0, 122, 255, 0.06)", "var(--accent-subtle-bg)"),
    ("rgba(0, 122, 255, 0.45)", "color-mix(in srgb, var(--accent) 40%, transparent)"),
    ("rgba(0, 122, 255, 0.08)", "color-mix(in srgb, var(--accent) 10%, var(--surface-page))"),
    ("rgba(0, 122, 255, 0.15)", "var(--accent-focus-ring)"),
    ("rgba(0, 122, 255, 0.16)", "var(--accent-focus-ring)"),
    ("rgba(0, 122, 255, 0.2)", "color-mix(in srgb, var(--accent) 22%, transparent)"),
    ("rgba(0, 122, 255, 0.38)", "color-mix(in srgb, var(--accent) 18%, transparent)"),
    ("rgba(0, 122, 255, 0.42)", "color-mix(in srgb, var(--accent) 22%, transparent)"),
    ("rgba(0, 122, 255, 0.55)", "color-mix(in srgb, var(--accent) 32%, transparent)"),
    ("#007aff", "var(--accent)"),
    ("#005fcc", "var(--accent-press)"),
    ("color: #fff", "color: var(--text-on-accent)"),
    ("background: #ffffff", "background: var(--surface-elevated)"),
    ("background: #fff", "background: var(--surface-elevated)"),
    ("border-inline-start: 2px solid #fff", "border-inline-start: 2px solid var(--text-on-accent)"),
    ("border-block-end: 2px solid #fff", "border-block-end: 2px solid var(--text-on-accent)"),
    (".course-page__modal-backdrop {\n  position: fixed;\n  inset: 0;\n  background: rgba(15, 23, 42, 0.35);",
     ".course-page__modal-backdrop {\n  position: fixed;\n  inset: 0;\n  background: var(--backdrop-scrim);"),
    (".course-page__modal {\n  width: min(560px, 100%);\n  background: var(--surface-elevated);\n  border-radius: 22px;\n  box-shadow: var(--shadow-diffused);",
     ".course-page__modal {\n  width: min(560px, 100%);\n  background: var(--surface-elevated);\n  border-radius: 22px;\n  box-shadow: var(--shadow-modal);"),
    (".course-page__doc-card {\n  display: flex;\n  flex-wrap: wrap;\n  align-items: center;\n  justify-content: space-between;\n  gap: 1rem 1.25rem;\n  padding: 1.5rem 1.5rem;\n  border-radius: 16px;\n  background: var(--surface-elevated);\n  border: var(--border-hairline);\n  box-shadow: var(--shadow-diffused);",
     ".course-page__doc-card {\n  display: flex;\n  flex-wrap: wrap;\n  align-items: center;\n  justify-content: space-between;\n  gap: 1rem 1.25rem;\n  padding: 1.5rem 1.5rem;\n  border-radius: 16px;\n  background: var(--surface-elevated);\n  border: var(--border-hairline);\n  box-shadow: none;"),
    (".course-page__doc-card:hover {\n  box-shadow:\n    0 24px 48px -22px rgba(15, 23, 42, 0.1),\n    0 1px 0 rgba(15, 23, 42, 0.04);\n}",
     ".course-page__doc-card:hover {\n  box-shadow: var(--shadow-hover);\n  border-color: color-mix(in srgb, var(--text-primary) 8%, var(--border-color));\n}"),
    (".course-page__banner {\n  background: var(--surface-elevated);\n  color: var(--text-primary);\n  border-radius: 20px;\n  padding: 1.85rem 1.6rem;\n  border: var(--border-hairline);\n  box-shadow: var(--shadow-diffused);\n  border-inline-start: 3px solid var(--brand-blue);",
     ".course-page__banner {\n  background: var(--surface-elevated);\n  color: var(--text-primary);\n  border-radius: 20px;\n  padding: 1.85rem 1.6rem;\n  border: var(--border-hairline);\n  box-shadow: var(--shadow-ambient);\n  border-inline-start: 2px solid color-mix(in srgb, var(--accent) 55%, var(--border-color));"),
    (".course-page__banner:hover {\n  box-shadow:\n    0 28px 56px -18px rgba(15, 23, 42, 0.1),\n    0 1px 0 rgba(15, 23, 42, 0.04);\n}",
     ".course-page__banner:hover {\n  box-shadow: var(--shadow-hover);\n}"),
    (".course-page__back-btn {\n  border: 1px solid var(--brand-blue);",
     ".course-page__back-btn {\n  border: var(--border-hairline);\n  color: var(--text-primary);"),
    (".course-page__back-btn {\n  border: var(--border-hairline);\n  color: var(--text-primary);\n  background: transparent;\n  color: var(--brand-blue);",
     ".course-page__back-btn {\n  border: var(--border-hairline);\n  background: transparent;\n  color: var(--accent);"),
    (".course-page__back-btn:hover {\n  background: var(--active-bg-tint);\n  box-shadow: 0 8px 24px -8px rgba(15, 23, 42, 0.08);\n}",
     ".course-page__back-btn:hover {\n  background: var(--active-bg-tint);\n  box-shadow: var(--shadow-ambient);\n}"),
    (".course-page__set-card {\n  border: var(--border-hairline);\n  border-radius: 14px;\n  background: var(--surface-elevated);\n  box-shadow: var(--shadow-diffused);",
     ".course-page__set-card {\n  border: var(--border-hairline);\n  border-radius: 14px;\n  background: var(--surface-elevated);\n  box-shadow: none;"),
    (".course-page__question-card {\n  border: var(--border-hairline);\n  border-radius: 14px;\n  background: var(--surface-elevated);\n  box-shadow: var(--shadow-diffused);",
     ".course-page__question-card {\n  border: var(--border-hairline);\n  border-radius: 14px;\n  background: var(--surface-elevated);\n  box-shadow: none;"),
    (".course-page__mode-btn--active {\n  color: var(--text-primary);\n  background: var(--surface-elevated);\n  box-shadow: 0 4px 10px -6px rgba(15, 23, 42, 0.28);\n}",
     ".course-page__mode-btn--active {\n  color: var(--text-primary);\n  background: var(--surface-elevated);\n  box-shadow: var(--shadow-ambient);\n}"),
    (".course-page__quiz-action-bar {\n  position: fixed;\n  inset-inline: clamp(0.75rem, 2.5vw, 1.5rem);\n  inset-block-end: calc(0.85rem + env(safe-area-inset-bottom, 0px));\n  z-index: 950;\n  border-radius: 16px;\n  background: var(--surface-elevated);\n  border: var(--border-hairline);\n  box-shadow:\n    0 16px 40px -18px rgba(15, 23, 42, 0.24),\n    0 1px 0 rgba(15, 23, 42, 0.04);",
     ".course-page__quiz-action-bar {\n  position: fixed;\n  inset-inline: clamp(0.75rem, 2.5vw, 1.5rem);\n  inset-block-end: calc(0.85rem + env(safe-area-inset-bottom, 0px));\n  z-index: 950;\n  border-radius: 16px;\n  background: var(--surface-elevated);\n  border: var(--border-hairline);\n  box-shadow: var(--shadow-modal);"),
    (".course-page__quiz-generate-btn {\n  border: none;\n  border-radius: 999px;\n  background: var(--accent);\n  color: var(--text-on-accent);\n  font-family: inherit;\n  font-size: 0.9rem;\n  font-weight: 600;\n  padding-block: 0.58rem;\n  padding-inline: 1rem;\n  cursor: pointer;\n  transition:\n    filter 0.22s var(--ease-fluid),\n    transform 0.22s var(--ease-fluid),\n    box-shadow 0.22s var(--ease-fluid);\n  box-shadow: 0 8px 24px -10px rgba(0, 122, 255, 0.55);",
     ".course-page__quiz-generate-btn {\n  border: none;\n  border-radius: 999px;\n  background: var(--accent);\n  color: var(--text-on-accent);\n  font-family: inherit;\n  font-size: 0.9rem;\n  font-weight: 600;\n  padding-block: 0.58rem;\n  padding-inline: 1rem;\n  cursor: pointer;\n  transition:\n    background-color 0.22s var(--ease-fluid),\n    transform 0.22s var(--ease-fluid),\n    box-shadow 0.22s var(--ease-fluid);\n  box-shadow: var(--shadow-ambient);"),
    (".course-page__quiz-generate-btn:hover:not(:disabled) {\n  filter: brightness(1.05);\n  transform: translateY(-1px);\n}",
     ".course-page__quiz-generate-btn:hover:not(:disabled) {\n  filter: none;\n  background: var(--accent-hover);\n  transform: translateY(-1px);\n}"),
    (".course-page__quiz-generate-btn:focus-visible {\n  outline: 2px solid var(--accent);\n  outline-offset: 3px;\n}",
     ".course-page__quiz-generate-btn:focus-visible {\n  outline: 2px solid var(--accent);\n  outline-offset: 3px;\n}"),
    (".course-page__modal-submit {\n  color: var(--text-on-accent);\n  border: none;\n  background: var(--brand-blue);\n  box-shadow: 0 6px 20px -4px rgba(0, 122, 255, 0.45);",
     ".course-page__modal-submit {\n  color: var(--text-on-accent);\n  border: none;\n  background: var(--accent);\n  box-shadow: var(--shadow-ambient);"),
    (".course-page__modal-submit:hover:not(:disabled) {\n  filter: brightness(1.05);\n}",
     ".course-page__modal-submit:hover:not(:disabled) {\n  filter: none;\n  background: var(--accent-hover);\n}"),
    (".course-page__modal-submit--danger {\n  background: #b91c1c;\n  box-shadow: 0 6px 20px -4px rgba(185, 28, 28, 0.45);",
     ".course-page__modal-submit--danger {\n  background: var(--danger-strong);\n  box-shadow: var(--shadow-ambient);"),
    (".quiz-loading-overlay {\n  position: fixed;\n  inset: 0;\n  z-index: 1200;\n  background: rgba(12, 18, 30, 0.28);",
     ".quiz-loading-overlay {\n  position: fixed;\n  inset: 0;\n  z-index: 1200;\n  background: var(--backdrop-scrim);"),
    (".quiz-loading-overlay__content {\n  width: min(420px, 100%);\n  border-radius: 18px;\n  border: var(--border-hairline);\n  background: color-mix(in srgb, var(--surface-elevated) 90%, #fff);\n  box-shadow:\n    0 24px 48px -20px rgba(15, 23, 42, 0.35),\n    0 1px 0 rgba(255, 255, 255, 0.45) inset;",
     ".quiz-loading-overlay__content {\n  width: min(420px, 100%);\n  border-radius: 18px;\n  border: var(--border-hairline);\n  background: color-mix(in srgb, var(--surface-elevated) 94%, var(--surface-page));\n  box-shadow: var(--shadow-modal);"),
]

# Apply generic subs first (order: longer rgba strings before shorter - already ordered)
# But #007aff already replaced by var accent - good

for old, new in subs:
    if old in t:
        t = t.replace(old, new, 1)
    else:
        print("MISSING BLOCK:", old[:60])

# Fix quiz-btn block if still has filter / old shadows - read patterns
t = t.replace(
    ".course-page__quiz-btn:hover:not(:disabled) {\n  filter: brightness(1.05);\n",
    ".course-page__quiz-btn:hover:not(:disabled) {\n  filter: none;\n  background: var(--accent-hover);\n",
)
t = t.replace(
    ".course-page__upload-btn:hover {\n  filter: brightness(1.05);\n",
    ".course-page__upload-btn:hover {\n  filter: none;\n  background: var(--accent-hover);\n",
)
t = t.replace("font-weight: 650;", "font-weight: 600;")

# Danger / success text tokens where still hex
for old, new in [
    ("color: #991b1b", "color: var(--danger-strong)"),
    ("color: #b91c1c", "color: var(--danger-strong)"),
    ("color: #166534", "color: var(--success-fg)"),
    ("outline: 2px solid var(--accent);", "outline: 2px solid var(--accent);"),  # no-op safeguard
]:
    t = t.replace(old, new)

path.write_text(t, encoding="utf-8")
print("course patched")
