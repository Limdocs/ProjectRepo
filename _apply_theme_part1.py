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

def apply_common(path: Path, extra: list[tuple[str, str]]):
    t = path.read_text(encoding="utf-8")
    t = ease_replace(t)
    for old, new in extra:
        if old not in t:
            pass  # continue; optional warn
        t = t.replace(old, new)
    path.write_text(t, encoding="utf-8")

ROOT = Path(r"c:\Projects\Limdocs\frontend\src\pages")

# --- HomePage.css ---
home_extra = [
    ("background: rgba(0, 122, 255, 0.06);", "background: var(--accent-subtle-bg);"),
    ("border-color: rgba(0, 122, 255, 0.28);", "border-color: var(--accent-border-soft);"),
    (".home-page__course-card {\n  width: 100%;\n  height: 100%;\n  min-height: 108px;\n  border: var(--border-hairline);\n  border-radius: 18px;\n  background: var(--surface-elevated);\n  padding: 1.1rem 1rem;\n  text-align: start;\n  font-family: inherit;\n  cursor: pointer;\n  display: flex;\n  align-items: center;\n  gap: 0.85rem;\n  box-shadow: var(--shadow-diffused);",
     ".home-page__course-card {\n  width: 100%;\n  height: 100%;\n  min-height: 108px;\n  border: var(--border-hairline);\n  border-radius: 18px;\n  background: var(--surface-elevated);\n  padding: 1.1rem 1rem;\n  text-align: start;\n  font-family: inherit;\n  cursor: pointer;\n  display: flex;\n  align-items: center;\n  gap: 0.85rem;\n  box-shadow: none;"),
    (".home-page__course-card:hover {\n  transform: translateY(-3px);\n  border-color: rgba(0, 122, 255, 0.22);\n  box-shadow:\n    0 28px 56px -16px rgba(15, 23, 42, 0.12),\n    0 1px 0 rgba(15, 23, 42, 0.04);\n}",
     ".home-page__course-card:hover {\n  transform: translateY(-1px);\n  border-color: var(--accent-border-soft);\n  box-shadow: var(--shadow-hover);\n}"),
    (".home-page__modal-backdrop {\n  position: fixed;\n  inset: 0;\n  background: rgba(15, 23, 42, 0.35);",
     ".home-page__modal-backdrop {\n  position: fixed;\n  inset: 0;\n  background: var(--backdrop-scrim);"),
    (".home-page__modal {\n  width: min(560px, 100%);\n  background: var(--surface-elevated);\n  border-radius: 22px;\n  box-shadow: var(--shadow-diffused);",
     ".home-page__modal {\n  width: min(560px, 100%);\n  background: var(--surface-elevated);\n  border-radius: 22px;\n  box-shadow: var(--shadow-modal);"),
    ("  box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.18);", "  box-shadow: 0 0 0 3px var(--accent-focus-ring);"),
    (".home-page__course-card--create {\n  border-style: dashed;\n  border-color: rgba(0, 122, 255, 0.28);",
     ".home-page__course-card--create {\n  border-style: dashed;\n  border-color: var(--accent-border-soft);"),
    (".home-page__course-card--create:hover {\n  background: var(--active-bg-tint);\n  border-color: rgba(0, 122, 255, 0.45);",
     ".home-page__course-card--create:hover {\n  background: var(--active-bg-tint);\n  border-color: color-mix(in srgb, var(--accent) 42%, var(--surface-elevated));"),
    (".home-page__course-card-icon-wrap--create {\n  background: transparent;\n  border: 1px dashed rgba(0, 122, 255, 0.35);",
     ".home-page__course-card-icon-wrap--create {\n  background: transparent;\n  border: 1px dashed color-mix(in srgb, var(--accent) 36%, var(--surface-elevated));"),
    (".home-page__course-card--create:hover .home-page__course-card-icon-wrap--create {\n  border-color: rgba(0, 122, 255, 0.55);\n  background: rgba(255, 255, 255, 0.8);",
     ".home-page__course-card--create:hover .home-page__course-card-icon-wrap--create {\n  border-color: color-mix(in srgb, var(--accent) 52%, var(--surface-elevated));\n  background: color-mix(in srgb, var(--surface-elevated) 92%, transparent);"),
    (".home-page__courses-error {\n  margin: 0.35rem 0;\n  color: #b91c1c;",
     ".home-page__courses-error {\n  margin: 0.35rem 0;\n  color: var(--danger-strong);"),
    (".home-page__course-delete-btn:focus-visible {\n  outline: 2px solid #b91c1c;",
     ".home-page__course-delete-btn:focus-visible {\n  outline: 2px solid var(--danger-strong);"),
    (".home-page__modal-submit {\n  color: #fff;\n  border: none;\n  background: var(--brand-blue);\n  box-shadow: 0 6px 20px -4px rgba(0, 122, 255, 0.45);",
     ".home-page__modal-submit {\n  color: var(--text-on-accent);\n  border: none;\n  background: var(--accent);\n  box-shadow: var(--shadow-ambient);"),
    (".home-page__modal-submit:hover {\n  filter: brightness(1.05);\n}",
     ".home-page__modal-submit:hover {\n  background: var(--accent-hover);\n  filter: none;\n}"),
    (".home-page__modal-submit--danger {\n  background: #b91c1c;\n  box-shadow: 0 6px 20px -4px rgba(185, 28, 28, 0.45);",
     ".home-page__modal-submit--danger {\n  background: var(--danger-strong);\n  box-shadow: var(--shadow-ambient);"),
    (".home-page__modal-submit--danger {\n  background: var(--danger-strong);\n  box-shadow: var(--shadow-ambient);\n}",
     ".home-page__modal-submit--danger {\n  background: var(--danger-strong);\n  box-shadow: var(--shadow-ambient);\n}\n\n.home-page__modal-submit--danger:hover {\n  background: color-mix(in srgb, var(--danger-strong) 92%, #000);\n}"),
    (".home-page__modal-error {\n  margin: 0.25rem 0 0;\n  color: #b91c1c;",
     ".home-page__modal-error {\n  margin: 0.25rem 0 0;\n  color: var(--danger-strong);"),
    (".home-page__logout:hover {\n  background: var(--surface-elevated);\n  border-color: rgba(0, 122, 255, 0.28);\n  box-shadow: var(--shadow-diffused);\n}",
     ".home-page__logout:hover {\n  background: var(--surface-elevated);\n  border-color: var(--accent-border-soft);\n  box-shadow: var(--shadow-ambient);\n}"),
]
apply_common(ROOT / "HomePage.css", home_extra)

# --- LoginPage.css ---
login_extra = [
    ("  box-shadow:\n    0 1px 2px rgba(0, 0, 0, 0.06),\n    0 16px 40px -12px rgba(0, 122, 255, 0.35);",
     "  box-shadow: var(--shadow-ambient);"),
    (".login-page__feedback--success {\n  background: #ecfdf5;\n  color: #065f46;\n  border: 1px solid #6ee7b7;\n}",
     ".login-page__feedback--success {\n  background: var(--success-bg);\n  color: var(--success-fg);\n  border: 1px solid var(--success-border);\n}"),
    (".login-page__feedback--error {\n  background: #fef2f2;\n  color: #991b1b;\n  border: 1px solid #fca5a5;\n}",
     ".login-page__feedback--error {\n  background: var(--danger-bg);\n  color: var(--danger-fg);\n  border: 1px solid var(--danger-border);\n}"),
    ("  color: #94a3b8;", "  color: var(--text-muted);"),
    ("  border-color: rgba(0, 122, 255, 0.28);", "  border-color: var(--accent-border-soft);"),
    ("  box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.18);", "  box-shadow: 0 0 0 3px var(--accent-focus-ring);"),
    ("  border-color: #dc2626;\n  box-shadow: 0 0 0 3px rgba(220, 38, 38, 0.15);", "  border-color: var(--danger-strong);\n  box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger-strong) 22%, transparent);"),
    ("  color: #b91c1c;", "  color: var(--danger-strong);"),
    ("  color: #fff;", "  color: var(--text-on-accent);"),
    ("  box-shadow:\n    0 1px 2px rgba(0, 0, 0, 0.06),\n    0 12px 28px -6px rgba(0, 122, 255, 0.38);",
     "  box-shadow: var(--shadow-ambient);"),
    ("  box-shadow:\n    0 1px 2px rgba(0, 0, 0, 0.06),\n    0 16px 36px -6px rgba(0, 122, 255, 0.42);",
     "  box-shadow: var(--shadow-hover);"),
    (".login-page__submit:hover {\n  filter: brightness(1.05);\n  box-shadow:\n    0 1px 2px rgba(0, 0, 0, 0.06),\n    0 16px 36px -6px rgba(0, 122, 255, 0.42);\n  transform: translateY(-1px);\n}",
     ".login-page__submit:hover {\n  filter: none;\n  background: var(--accent-hover);\n  box-shadow: var(--shadow-hover);\n  transform: translateY(-1px);\n}"),
    ("  border-bottom-color: rgba(0, 122, 255, 0.35);", "  border-bottom-color: var(--accent-border-soft);"),
    (".login-page__logo {\n  display: inline-flex;\n  align-items: center;\n  justify-content: center;\n  width: 52px;\n  height: 52px;\n  border-radius: 14px;\n  background: var(--brand-blue);\n  color: #fff;",
     ".login-page__logo {\n  display: inline-flex;\n  align-items: center;\n  justify-content: center;\n  width: 52px;\n  height: 52px;\n  border-radius: 14px;\n  background: var(--accent);\n  color: var(--text-on-accent);"),
]
apply_common(ROOT / "LoginPage.css", login_extra)

# Add utility class for label spacing (used from JSX)
login_path = ROOT / "LoginPage.css"
lt = login_path.read_text(encoding="utf-8")
if ".login-page__label--spaced" not in lt:
    lt += "\n\n.login-page__label--spaced {\n  margin-bottom: 0.75rem;\n}\n"
login_path.write_text(lt, encoding="utf-8")

print("home + login patched")
