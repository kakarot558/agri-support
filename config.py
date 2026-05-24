import os
from datetime import timedelta

basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = 'agri-fortress-secret-key-change-in-prod-2024'
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(basedir, 'instance', 'agristore.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Upload settings
    UPLOAD_FOLDER = os.path.join(basedir, 'static', 'uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max upload
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

    # Security
    WTF_CSRF_ENABLED = True
    SESSION_COOKIE_SECURE = False  # Set True in production with HTTPS
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    # ── Facebook API ──────────────────────────────────────────────────────────
    FACEBOOK_PAGE_ID      = '1022925337569355'
    FACEBOOK_ACCESS_TOKEN = (
        'EAALHWunapggBQ76PXQ0TkZBfGod2P7spWPqjRAb9mu4bXjEzKOlrVYIjjAXsNc3fk'
        'UGGeSJQPm3EA1YZAz1j4K9FU7L7uOqfl97AIOGjy0iniI8CxU5lWVWZBsduxG5Dg99G'
        'ljqpqY18L2nZBpGggF3vgqJOprlt9MBD7QNCGPBynjnjcqQuYgTdtG2NvZCbh0JZAlA0hW'
    )

    # Store Info
    STORE_NAME     = 'Agri-support supply Co.'
    STORE_WEBSITE  = 'agri-support.ph'
    WATERMARK_TEXT = 'Agri-support'

    # Rate limiting
    RATELIMIT_DEFAULT     = '200 per day;50 per hour'
    RATELIMIT_STORAGE_URL = 'memory://'

    # Image signing
    IMAGE_SIGN_SECRET  = 'img-sign-secret-2024'
    IMAGE_TOKEN_EXPIRY = 3600  # 1 hour

    # ── J&T Express Official API ─────────────────────────────────────────────
    # Register FREE at https://developer.jet.co.id (sandbox)
    # Production keys require a Cooperation Agreement with J&T Philippines
    JNT_API_KEY     = ''   # fill in when you have your key
    JNT_PRIVATE_KEY = ''   # fill in when you have your key
    JNT_CUSTOMER_ID = ''   # fill in when you have your key
    JNT_PRODUCTION  = ''   # set to any non-empty string to use production endpoint

    # ── Fallback Tracking (TrackingMore — 100 free calls/month) ──────────────
    # https://www.trackingmore.com/signup.html
    TRACKINGMORE_API_KEY = 'vzh3letm-nmcc-dx9e-qwou-tn922brwfzy7'

    # ── Payment Details (GCash / Bank Transfer) ───────────────────────────────
    GCASH_NUMBER      = '0992 963 4997'
    GCASH_NAME        = 'erwil m olivare jr'
    BANK_NAME         = 'BDO'
    BANK_ACCOUNT      = '1234-5678-9012'
    BANK_ACCOUNT_NAME = 'erwil m olivare jr'
    PAYMENT_REFERENCE_NOTE = (
        'Please use your order number as the reference/note when sending payment.'
    )

    JT_CARRIER_CODE = 'jtexpress-ph'

    # ── Shippo (rates + labels + tracking, 25 free shipments/month) ──────────
    # https://goshippo.com/signup
    SHIPPO_API_KEY = 'shippo_test_87004d0aa0b3f734c04525a0fe6b68cd97e41ad9'

    # ── EasyPost (secondary backup) ────────────────────────────────────────────
    # https://www.easypost.com/signup
    EASYPOST_API_KEY = ''  # fill in when you have your key

    # ── COD limit (PHP) ───────────────────────────────────────────────────────
    MAX_COD_AMOUNT = 10000.0

    # ── Shipping fees (PHP) — flat fallback when APIs unavailable ─────────────
    SHIP_FEES = {
        'J&T':    {'fee': 100.0, 'days': '2-3', 'label': 'J&T Express PH'},
        'MSC':    {'fee': 150.0, 'days': '3-5', 'label': 'MSC Shipping'},
        'LBC':    {'fee': 120.0, 'days': '2-4', 'label': 'LBC Express'},
        'Pickup': {'fee': 0.0,   'days': '0',   'label': 'Store Pickup'},
    }
