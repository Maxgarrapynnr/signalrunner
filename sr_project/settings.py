"""
sr_project/settings.py

Single-owner SignalRunner. Mirrors PyRunner: env-driven, Django + django-q2 +
SQLite, single data volume, Fernet-encrypted secrets. No heavy system binary
(unlike DocRunner's LibreOffice) — just casabourse + pandas + requests.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# SECRET_KEY — safe fallback so management commands (migrate, collectstatic)
# work during container startup before env vars are injected by the PaaS.
import hashlib as _h, socket as _s
_fallback_key = _h.sha256((_s.gethostname() + "signalrunner").encode()).hexdigest()
SECRET_KEY = os.environ.get("SECRET_KEY") or _fallback_key

# ENCRYPTION_KEY — Fernet, evaluated lazily by the Secret model.
_enc_key_str = os.environ.get("ENCRYPTION_KEY", "")
ENCRYPTION_KEY = _enc_key_str.encode() if _enc_key_str else b""

DEBUG = os.environ.get("DEBUG", "False").lower() == "true"
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = [
    o for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_q",
    "signalrunner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "sr_project.urls"
WSGI_APPLICATION = "sr_project.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ── Database ──────────────────────────────────────────────────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "signalrunner.sqlite3",
        "OPTIONS": {"timeout": 20},
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Files ─────────────────────────────────────────────────────────────────────
MEDIA_ROOT = DATA_DIR / "media"
MEDIA_URL = "/media/"
STATIC_URL = "/static/"
STATIC_ROOT = DATA_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# ── django-q2 — worker + scheduler ───────────────────────────────────────────
Q_CLUSTER = {
    "name": "signalrunner",
    "workers": int(os.environ.get("Q_WORKERS", "2")),
    "timeout": 120,
    "retry": 300,   # > timeout — fixes the warning we saw in the backend tests
    "max_attempts": 1,   # we manage our own retries in delivery.py
    "catch_up": False,   # missed schedules don't stampede on restart
    "orm": "default",    # SQLite as the broker — no Redis needed
    "label": "SignalRunner Tasks",
}

# ── Market-data settings ──────────────────────────────────────────────────────
MARKETDATA_PROVIDER = os.environ.get("MARKETDATA_PROVIDER", "casabourse")
MARKETDATA_FALLBACK_PROVIDER = os.environ.get("MARKETDATA_FALLBACK_PROVIDER", "yahoo")
MARKETDATA_CACHE_TTL_SECONDS = int(os.environ.get("MARKETDATA_CACHE_TTL_SECONDS", "300"))
SNAPSHOT_RETENTION_DAYS = int(os.environ.get("SNAPSHOT_RETENTION_DAYS", "30"))

# ── Security ──────────────────────────────────────────────────────────────────
if not DEBUG:
    SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "True").lower() == "true"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "Africa/Casablanca")  # UTC+1, BVC market time
USE_I18N = True
USE_TZ = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
