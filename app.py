import os
import threading
import time
import hmac
import hashlib
import requests
import logging
log = logging.getLogger(__name__)
import random
from datetime import datetime, timedelta, timezone
from functools import wraps
from io import BytesIO

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, send_file, abort, session, Response)
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from werkzeug.utils import secure_filename
from apscheduler.schedulers.background import BackgroundScheduler
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from config import Config
from models import (db, User, Product, Campaign, FacebookPostLog, AutomationLog,
                   Order, OrderItem, OrderEvent,
                   Shipment, TrackingLog, CODTransaction, CourierPerformance)
from services.shipping.shipping_service import ShippingService

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)
csrf = CSRFProtect(app)

# ─── Shipping Service (singleton, uses config/env vars) ───────────────────────
def _get_shipping_service():
    """Lazy singleton — init after app context."""
    if not hasattr(app, '_shipping_svc'):
        app._shipping_svc = ShippingService({
            'JNT_API_KEY':          app.config.get('JNT_API_KEY', ''),
            'JNT_PRIVATE_KEY':      app.config.get('JNT_PRIVATE_KEY', ''),
            'JNT_CUSTOMER_ID':      app.config.get('JNT_CUSTOMER_ID', ''),
            'JNT_PRODUCTION':       app.config.get('JNT_PRODUCTION', ''),
            'TRACKINGMORE_API_KEY': app.config.get('TRACKINGMORE_API_KEY', ''),
            'SHIPPO_API_KEY':       app.config.get('SHIPPO_API_KEY', ''),
            'EASYPOST_API_KEY':     app.config.get('EASYPOST_API_KEY', ''),
            'MAX_COD_AMOUNT':       app.config.get('MAX_COD_AMOUNT', 10000),
        })
    return app._shipping_svc

# ─── Ensure instance & upload dirs exist at import time (fixes Windows SQLite) ──
os.makedirs(os.path.join(os.path.abspath(os.path.dirname(__file__)), 'instance'), exist_ok=True)
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'warning'

# Image token serializer
serializer = URLSafeTimedSerializer(app.config['IMAGE_SIGN_SECRET'])

# ─── Security Headers ────────────────────────────────────────────────────────
@app.after_request
def add_security_headers(response):
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob:; "
        "connect-src 'self';"
    )
    return response

# ─── Login Manager ───────────────────────────────────────────────────────────
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ─── Helpers ─────────────────────────────────────────────────────────────────
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def generate_image_token(image_path):
    return serializer.dumps(image_path)

def verify_image_token(token, max_age=3600):
    try:
        return serializer.loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated or current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def log_automation(event_type, status, message, product_id=None, campaign_id=None):
    log = AutomationLog(
        event_type=event_type,
        product_id=product_id,
        campaign_id=campaign_id,
        status=status,
        message=message
    )
    db.session.add(log)
    db.session.commit()

def add_watermark(image_path):
    """Legacy stub — watermark now applied at serve-time."""
    pass


def serve_watermarked_image(full_path):
    """
    Serve image with STRONG watermark:
    - Tiled diagonal repeating pattern across entire image
    - Large bold centre stamp
    - Corner stamp
    Never modifies the original file.
    """
    import io as _io
    if not PIL_AVAILABLE:
        return send_file(full_path)
    try:
        img  = Image.open(full_path).convert('RGBA')
        w, h = img.size

        brand = app.config.get('WATERMARK_TEXT', 'AgriFortress')
        text  = f'© {brand}'
        fp    = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'

        # ── 1. Tiled diagonal repeating pattern ───────────────────────────
        tile_sz = max(200, min(w, h) // 2)
        try:    tf = ImageFont.truetype(fp, max(20, tile_sz // 7))
        except: tf = ImageFont.load_default()

        tb = ImageDraw.Draw(Image.new('RGBA',(1,1))).textbbox((0,0), text, font=tf)
        tw, th = tb[2]-tb[0], tb[3]-tb[1]

        # Render text onto small transparent canvas then rotate 35 deg
        txt_img = Image.new('RGBA', (tw+24, th+20), (0,0,0,0))
        ImageDraw.Draw(txt_img).text((12,10), text, font=tf, fill=(255,255,255,72))
        txt_rot = txt_img.rotate(35, expand=True)

        # Tile across full image
        tile_layer = Image.new('RGBA', (w, h), (0,0,0,0))
        rw, rh = txt_rot.size
        step_x = rw + 30
        step_y = rh + 30
        for ty in range(-step_y, h + step_y, step_y):
            for tx in range(-step_x, w + step_x, step_x):
                tile_layer.paste(txt_rot, (tx, ty), txt_rot)

        # ── 2. Large bold centre stamp ────────────────────────────────────
        c_layer = Image.new('RGBA', (w, h), (0,0,0,0))
        cd = ImageDraw.Draw(c_layer)
        try:    cf = ImageFont.truetype(fp, max(32, w // 7))
        except: cf = ImageFont.load_default()

        cb2 = cd.textbbox((0,0), text, font=cf)
        cw, ch = cb2[2]-cb2[0], cb2[3]-cb2[1]
        cx, cy = w // 2, h // 2
        px, py = 32, 16

        cd.rounded_rectangle(
            [cx-cw//2-px, cy-ch//2-py, cx+cw//2+px, cy+ch//2+py],
            radius=18, fill=(0,0,0,130))
        cd.text((cx-cw//2+2, cy-ch//2+2), text, font=cf, fill=(0,0,0,130))
        cd.text((cx-cw//2,   cy-ch//2),   text, font=cf, fill=(255,255,255,210))

        # ── 3. Bottom-right corner stamp ──────────────────────────────────
        k_layer = Image.new('RGBA', (w, h), (0,0,0,0))
        kd = ImageDraw.Draw(k_layer)
        try:    kf = ImageFont.truetype(fp, max(16, w // 16))
        except: kf = ImageFont.load_default()

        kb = kd.textbbox((0,0), text, font=kf)
        kw, kh = kb[2]-kb[0], kb[3]-kb[1]
        kp = 12
        kx = w - kw - kp*2 - 14
        ky = h - kh - kp*2 - 14
        kd.rounded_rectangle([kx, ky, kx+kw+kp*2, ky+kh+kp*2], radius=10, fill=(0,0,0,170))
        kd.text((kx+kp+1, ky+kp+1), text, font=kf, fill=(0,0,0,120))
        kd.text((kx+kp,   ky+kp),   text, font=kf, fill=(255,255,255,245))

        # ── 4. Composite and serve ─────────────────────────────────────────
        result = img.copy()
        result = Image.alpha_composite(result, tile_layer)
        result = Image.alpha_composite(result, c_layer)
        result = Image.alpha_composite(result, k_layer)
        result = result.convert('RGB')

        if result.width > 960:
            ratio  = 960 / result.width
            result = result.resize((960, int(result.height * ratio)), Image.LANCZOS)

        buf = _io.BytesIO()
        result.save(buf, format='JPEG', quality=85, optimize=True)
        buf.seek(0)
        response = Response(buf, mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma']        = 'no-cache'
        return response
    except Exception as e:
        print(f'[WATERMARK] {e}')
        return send_file(full_path)

def generate_ai_caption(product, tone='friendly'):
    tones = {
        'urgent': f"""🚨 LIMITED TIME OFFER! 🚨

⏰ Don't miss out on {product.name}!
Was: ₱{product.original_price:,.2f}
NOW: ₱{product.discounted_price:,.2f} ({product.discount_percentage:.0f}% OFF!)

Perfect for {product.crop_type or 'all'} farming.
⚠️ Only {product.stock_quantity} pcs left in stock!

📞 Order NOW before it's gone!
#{product.category.replace(' ', '')} #LimitedStock #AgriFortress""",

        'friendly': f"""🌾 Hello, Mahal na Magsasaka! 🌾

Great news! {product.name} is now on SALE!
📦 Original Price: ₱{product.original_price:,.2f}
💚 Sale Price: ₱{product.discounted_price:,.2f}

✅ Perfect for {product.crop_type or 'your farm'}
✅ Season: {product.season_applicable or 'All Year'}
✅ Packaging: {product.packaging_type or 'Standard'}

Invest in quality for a better harvest! 🌱
#FarmLife #HighYield #AgriBusiness #AgriFortress""",

        'professional': f"""📣 PRODUCT ANNOUNCEMENT

{product.name}
Category: {product.category}
Crop Application: {product.crop_type or 'Multi-crop'}

💰 Special Discount Price: ₱{product.discounted_price:,.2f}
   (Regular Price: ₱{product.original_price:,.2f})
   Savings: {product.discount_percentage:.0f}% OFF

For bulk orders and inquiries, contact us directly.

#{product.category.replace(' ', '')} #AgriSupply #AgriFortress #Philippines""",

        'seasonal': f"""🌱 PLANTING SEASON SALE IS HERE! 🌱

Prepare your farm with the best!
✨ {product.name} — NOW ON SALE!

🏷️ Price: ₱{product.discounted_price:,.2f} (was ₱{product.original_price:,.2f})
💯 {product.discount_percentage:.0f}% DISCOUNT!

🌾 Best for: {product.crop_type or 'All crops'}
📅 Season: {product.season_applicable or 'Year-round'}

Make this season your most productive yet!
#PlantingSeason #HarvestReady #AgriFortress #Magsasaka""",

        'lowstock': f"""⚠️ ALMOST GONE! Last Few Stocks! ⚠️

{product.name} is selling FAST!
🔥 NOW: ₱{product.discounted_price:,.2f} (Save {product.discount_percentage:.0f}%!)

Only {product.stock_quantity} units remaining!
Once it's gone, it's gone! 😱

Order now at AgriFortress Supply Co.
📍 Visit us or message us today!

#LowStock #LastChance #AgriFortress #FarmSupply"""
    }
    return tones.get(tone, tones['friendly'])


# ──────────────────────────────────────────────────────────────────────────────
#  FACEBOOK ENGINE v3 — fixed
#  - v21.0 API  |  photo post first  |  text fallback
#  - Non-blocking threads (no time.sleep freeze)
#  - [FACEBOOK] prefix on every terminal log line
#  - /api/fb-post-now/<id> for one-click browser testing
# ──────────────────────────────────────────────────────────────────────────────

FB_API_VERSION = "v21.0"

def _fb(msg):
    print(f"[FACEBOOK] {msg}", flush=True)

def _fb_error(rdata):
    err = rdata.get('error', {})
    code = err.get('code', 0)
    msg  = err.get('message', 'Unknown error')
    hints = {
        190: "TOKEN EXPIRED/INVALID → run fb_test.py to refresh",
        100: "INVALID PAGE_ID → check config.py FACEBOOK_PAGE_ID",
        200: "PERMISSION DENIED → token needs pages_manage_posts",
        10:  "APP IN DEVELOPMENT MODE → switch to Live at developers.facebook.com",
        368: "PAGE RESTRICTED → check Meta Business Suite → Page Quality",
        4:   "RATE LIMIT → wait a few minutes",
    }
    return f"Error {code}: {msg}" + (f"  →  {hints[code]}" if code in hints else "")

def _fb_save_log(product_id, campaign_id, post_id, status, caption, msg, retry):
    """Thread-safe DB write — always opens a fresh app context."""
    try:
        with app.app_context():
            db.session.add(FacebookPostLog(
                product_id=product_id,
                campaign_id=campaign_id,
                post_id=post_id,
                status=status,
                caption=caption,
                response_message=(msg or '')[:2000],
                retry_count=retry,
                created_at=datetime.now(timezone.utc)
            ))
            db.session.commit()
    except Exception as e:
        _fb(f"DB log error: {e}")

def _fb_post_photo(page_id, token, caption, image_path):
    """POST /photos with image attached. Returns (post_id, error_str)."""
    ext  = image_path.rsplit('.', 1)[-1].lower()
    mime = {'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png',
            'gif':'image/gif','webp':'image/webp'}.get(ext, 'image/jpeg')
    try:
        with open(image_path, 'rb') as f:
            resp = requests.post(
                f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/photos",
                data={'caption': caption, 'published': 'true', 'access_token': token},
                files={'source': (os.path.basename(image_path), f, mime)},
                timeout=30
            )
        rdata = resp.json()
        _fb(f"Photo response: {rdata}")
        pid = rdata.get('post_id') or rdata.get('id')
        return (pid, None) if pid else (None, _fb_error(rdata))
    except Exception as e:
        return None, f"Photo exception: {e}"

def _fb_post_text(page_id, token, caption):
    """POST /feed text only. Returns (post_id, error_str)."""
    try:
        resp  = requests.post(
            f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/feed",
            data={'message': caption, 'access_token': token},
            timeout=15
        )
        rdata = resp.json()
        _fb(f"Text response: {rdata}")
        pid = rdata.get('id')
        return (pid, None) if pid else (None, _fb_error(rdata))
    except Exception as e:
        return None, f"Text exception: {e}"

def post_to_facebook(product, caption=None, campaign_id=None, retry=0):
    """Synchronous post. Always call via fb_post_async() from Flask routes."""
    token   = (app.config.get('FACEBOOK_ACCESS_TOKEN') or '').strip()
    page_id = (app.config.get('FACEBOOK_PAGE_ID') or '').strip()

    if not token or token == 'your_access_token':
        _fb("SKIPPED — FACEBOOK_ACCESS_TOKEN not set in config.py")
        return False, "Token not configured"
    if not page_id:
        _fb("SKIPPED — FACEBOOK_PAGE_ID not set in config.py")
        return False, "Page ID not configured"

    _fb(f"Posting '{product.name}' | page={page_id} | token=...{token[-12:]}")

    if not caption:
        # Check for a custom_caption attribute on the product object (set at runtime)
        runtime_cap = getattr(product, '_custom_caption', None)
        if runtime_cap:
            caption = runtime_cap
        else:
            caption = generate_ai_caption(product, 'lowstock' if product.is_low_stock else 'friendly')
    else:
        # A campaign custom caption was provided — inject the product name/price at the top
        # so each per-product post clearly shows what is being promoted.
        disc_info = ''
        if product.discount_percentage > 0:
            disc_info = f' (was ₱{product.original_price:,.2f}, {product.discount_percentage:.0f}% OFF)'
        stock_info = f'Stock: {product.stock_quantity} pcs available\n' if product.stock_quantity else ''
        product_header = (
            f'📦 {product.name}\n'
            f'💰 ₱{product.discounted_price:,.2f}{disc_info}\n'
            f'{stock_info}'
        )
        caption = product_header + '\n' + caption

    post_id  = None
    last_err = "No attempt made"

    # Try 1: photo post
    if product.image_path:
        img_path = os.path.join(app.root_path, 'static', 'uploads',
                                os.path.basename(product.image_path))
        if os.path.exists(img_path):
            _fb(f"Trying photo post | {img_path}")
            post_id, last_err = _fb_post_photo(page_id, token, caption, img_path)
            if post_id:
                _fb(f"✅ Photo posted! post_id={post_id}")
                _fb_save_log(product.id, campaign_id, post_id, 'success', caption, "photo OK", retry)
                log_automation('facebook_post_success', 'success',
                               f"Photo posted: {product.name} → {post_id}", product.id, campaign_id)
                return True, post_id
            _fb(f"Photo failed: {last_err} → trying text-only")

    # Try 2: text-only
    _fb("Trying text-only post")
    post_id, last_err = _fb_post_text(page_id, token, caption)
    if post_id:
        _fb(f"✅ Text posted! post_id={post_id}")
        _fb_save_log(product.id, campaign_id, post_id, 'success', caption, "text OK", retry)
        log_automation('facebook_post_success', 'success',
                       f"Text posted: {product.name} → {post_id}", product.id, campaign_id)
        return True, post_id

    # Both failed
    _fb(f"❌ Attempt {retry+1}/3 failed: {last_err}")

    if retry < 2:
        delay = 5 * (retry + 1)
        _fb(f"Retrying in {delay}s ...")
        _fb_save_log(product.id, campaign_id, None, 'pending_retry',
                     caption, f"retry {retry+1}: {last_err}", retry)
        def _do_retry():
            with app.app_context():
                p = Product.query.get(product.id)
                if p:
                    post_to_facebook(p, caption, campaign_id, retry + 1)
        t = threading.Timer(delay, _do_retry)
        t.daemon = True
        t.start()
        return False, f"Retry in {delay}s"

    _fb(f"❌ FINAL FAIL after 3 attempts: {last_err}")
    _fb_save_log(product.id, campaign_id, None, 'failed',
                 caption, f"3 attempts failed: {last_err}", retry)
    log_automation('facebook_post_failed', 'failed',
                   f"3 attempts failed for '{product.name}': {last_err}", product.id, campaign_id)
    return False, last_err


def fb_post_async(product_id, caption=None, campaign_id=None):
    """
    Non-blocking fire-and-forget. ALWAYS use this from Flask routes.
    Runs post_to_facebook in a daemon thread so Flask never freezes.
    """
    _fb(f"Queueing async post for product_id={product_id}")
    def _run():
        with app.app_context():
            p = Product.query.get(product_id)
            if p:
                post_to_facebook(p, caption, campaign_id)
            else:
                _fb(f"product_id={product_id} not found in DB")
    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ─── Scheduler Jobs ──────────────────────────────────────────────────────────
def check_campaigns():
    """Runs every 5 minutes — activates/ends campaigns and fires FB posts."""
    import os as _os3
    _db_path2 = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
    if _db_path2 and not _db_path2.startswith(':') and not _os3.path.exists(_db_path2):
        print('[SCHEDULER] DB not ready yet — skipping check_campaigns', flush=True)
        return
    with app.app_context():
        now = datetime.now()   # local PHT — campaigns stored as local time

        # Activate campaigns whose time has come
        for campaign in Campaign.query.filter(Campaign.status == 'Scheduled').all():
            s = campaign.start_date
            e = campaign.end_date
            # Strip tzinfo if present (stored naive)
            if hasattr(s, 'tzinfo') and s.tzinfo: s = s.replace(tzinfo=None)
            if hasattr(e, 'tzinfo') and e.tzinfo: e = e.replace(tzinfo=None)
            if s <= now <= e:
                campaign.status = 'Active'
                q = Product.query.filter(Product.is_active == True)
                if campaign.category_target:
                    q = q.filter(Product.category == campaign.category_target)
                if campaign.crop_target:
                    q = q.filter(Product.crop_type == campaign.crop_target)
                products = q.all()
                post_ids = []
                for product in products:
                    product.discount_percentage = campaign.discount_percentage
                    product.calculate_discounted_price()
                    if campaign.auto_post and product.auto_post_enabled and product.stock_quantity > 0:
                        post_ids.append(product.id)
                db.session.commit()
                log_automation('campaign_activated', 'success',
                               f'Campaign "{campaign.name}" activated — {len(products)} products',
                               campaign_id=campaign.id)
                # Resolve caption: use custom if set, else None (auto per product)
                campaign_caption = None
                cap_mode = getattr(campaign, 'caption_mode', None)
                custom_cap_val = getattr(campaign, 'custom_caption', None)
                if cap_mode == 'manual' and custom_cap_val:
                    campaign_caption = custom_cap_val
                for pid in post_ids:
                    fb_post_async(pid, caption=campaign_caption, campaign_id=campaign.id)  # NON-BLOCKING

        # End expired campaigns
        for campaign in Campaign.query.filter(Campaign.status == 'Active').all():
            e = campaign.end_date
            if hasattr(e, 'tzinfo') and e.tzinfo: e = e.replace(tzinfo=None)
            if now > e:
                campaign.status = 'Ended'
                q = Product.query.filter(Product.is_active == True)
                if campaign.category_target:
                    q = q.filter(Product.category == campaign.category_target)
                for product in q.all():
                    product.discount_percentage = 0
                    product.discounted_price = product.original_price
                    product.price = product.original_price
                db.session.commit()
                log_automation('campaign_ended', 'info',
                               f'Campaign "{campaign.name}" ended, prices restored',
                               campaign_id=campaign.id)


def check_scheduled_posts():
    """
    Runs every 60 seconds (local time).
    Finds products where scheduled_post_at <= NOW (local PHT) and fires them.
    Uses raw SQL for both SELECT and UPDATE to avoid any ORM session issues.
    """
    # Guard: make sure the DB file actually exists before querying
    import os as _os2
    _db_path = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
    if _db_path and not _db_path.startswith(':') and not _os2.path.exists(_db_path):
        print('[SCHEDULER] DB not ready yet — skipping check_scheduled_posts', flush=True)
        return
    with app.app_context():
        now_local = datetime.now()
        now_ts    = now_local.strftime('%Y-%m-%d %H:%M:%S')

        from sqlalchemy import text as _sqlt

        # Raw SQL select — SQLite string comparison works because ISO dates sort correctly
        rows = db.session.execute(_sqlt(
            "SELECT id, name, post_tone, recurring_enabled, recurring_days, "
            "scheduled_post_at, caption_mode, custom_caption "
            "FROM products "
            "WHERE post_status='scheduled' AND is_active=1 "
            "AND scheduled_post_at IS NOT NULL "
            "AND scheduled_post_at <= :now"
        ), {'now': now_ts}).fetchall()

        if not rows:
            return   # nothing due

        print(f"[SCHEDULER] {len(rows)} post(s) due at {now_ts}", flush=True)

        for row in rows:
            pid, pname, tone, recurring, rec_days, sat_raw, cap_mode, custom_cap = row
            tone = tone or 'friendly'

            # Parse the stored datetime (may be string on SQLite/Windows)
            if isinstance(sat_raw, str):
                sat = None
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M'):
                    try:
                        sat = datetime.strptime(sat_raw[:19], fmt); break
                    except ValueError:
                        continue
                if sat is None:
                    sat = now_local
            elif isinstance(sat_raw, datetime):
                sat = sat_raw
            else:
                sat = now_local

            # Build caption & fire Facebook post
            try:
                product = db.session.get(Product, pid)
                if product:
                    if cap_mode == 'manual' and custom_cap:
                        caption = custom_cap
                    else:
                        caption = generate_ai_caption(product, tone)
                    fb_post_async(pid, caption=caption)
                    print(f"[SCHEDULER] Queued FB post for '{pname}' (id={pid})", flush=True)
            except Exception as e:
                print(f"[SCHEDULER] Error generating caption for id={pid}: {e}", flush=True)

            # Update status via raw SQL — avoids any ORM session caching issues
            try:
                if recurring and (rec_days or 0) > 0:
                    next_dt = sat + timedelta(days=int(rec_days))
                    next_ts = next_dt.strftime('%Y-%m-%d %H:%M:%S')
                    db.session.execute(_sqlt(
                        "UPDATE products SET post_status='scheduled', "
                        "scheduled_post_at=:next, last_posted_at=:now WHERE id=:id"
                    ), {'next': next_ts, 'now': now_ts, 'id': pid})
                    log_automation('scheduled_post_fired', 'success',
                                   f'Recurring post fired for "{pname}" — next: {next_ts}', pid)
                    print(f"[SCHEDULER] Recurring '{pname}' → next at {next_ts}", flush=True)
                else:
                    db.session.execute(_sqlt(
                        "UPDATE products SET post_status='posted', "
                        "last_posted_at=:now WHERE id=:id"
                    ), {'now': now_ts, 'id': pid})
                    log_automation('scheduled_post_fired', 'success',
                                   f'Scheduled post fired for "{pname}"', pid)
                    print(f"[SCHEDULER] ✅ '{pname}' → posted", flush=True)

                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"[SCHEDULER] DB update error for id={pid}: {e}", flush=True)


# ─── Scheduler start ──────────────────────────────────────────────────────────
# Guard against Flask debug-mode double-start (Werkzeug reloader spawns a child
# process — only start the scheduler inside that child, not the parent watcher).
import os as _os
_start_scheduler = (not app.debug) or (_os.environ.get('WERKZEUG_RUN_MAIN') == 'true')

if _start_scheduler:
    scheduler = BackgroundScheduler(timezone='Asia/Manila')   # PHT — matches stored times
    scheduler.add_job(func=check_campaigns,       trigger='interval', minutes=5,
                      id='campaign_checker',        max_instances=1, coalesce=True)
    # Delay first run by 10s so init_db() finishes before scheduler queries DB
    from datetime import timedelta as _td
    scheduler.add_job(func=check_scheduled_posts, trigger='interval', seconds=60,
                      id='scheduled_post_checker', max_instances=1, coalesce=True,
                      next_run_time=datetime.now() + _td(seconds=10))
    scheduler.start()
    print("[SCHEDULER] Started — campaign_checker(5m) + scheduled_post_checker(60s)", flush=True)
else:
    scheduler = None
    print("[SCHEDULER] Skipped start (parent watcher process)", flush=True)



# ─── PUBLIC ROUTES ───────────────────────────────────────────────────────────
@app.route('/')
def index():
    page = request.args.get('page', 1, type=int)
    category = request.args.get('category', '')
    crop = request.args.get('crop', '')
    season = request.args.get('season', '')
    discounted = request.args.get('discounted', '')
    in_stock = request.args.get('in_stock', '')
    search = request.args.get('search', '')

    query = Product.query.filter(Product.is_active == True)
    if category:
        query = query.filter(Product.category == category)
    if crop:
        query = query.filter(Product.crop_type == crop)
    if season:
        query = query.filter(Product.season_applicable.contains(season))
    if discounted:
        query = query.filter(Product.discount_percentage > 0)
    if in_stock:
        query = query.filter(Product.stock_quantity > 0)
    if search:
        query = query.filter(Product.name.contains(search) | Product.description.contains(search))

    products = query.order_by(Product.created_at.desc()).paginate(page=page, per_page=12, error_out=False)
    categories = db.session.query(Product.category).distinct().all()
    crops = db.session.query(Product.crop_type).filter(Product.crop_type != None).distinct().all()
    seasons = db.session.query(Product.season_applicable).filter(Product.season_applicable != None).distinct().all()
    featured = Product.query.filter(
        Product.is_active == True,
        Product.discount_percentage > 0
    ).order_by(Product.discount_percentage.desc()).limit(3).all()

    return render_template('index.html',
                           products=products, categories=categories,
                           crops=crops, seasons=seasons, featured=featured,
                           current_filters=dict(category=category, crop=crop,
                                                season=season, discounted=discounted,
                                                in_stock=in_stock, search=search))


@app.route('/product/<int:product_id>')
def product_detail(product_id):
    product = Product.query.get_or_404(product_id)
    if not product.is_active:
        abort(404)
    related = Product.query.filter(
        Product.category == product.category,
        Product.id != product.id,
        Product.is_active == True
    ).limit(4).all()
    return render_template('product_detail.html', product=product, related=related)


@app.route('/secure-image/<int:product_id>')
def secure_image_by_id(product_id):
    """Serve watermarked product image by ID (no token needed — watermark IS the protection)."""
    product = Product.query.get_or_404(product_id)
    if not product.image_path:
        return redirect(url_for('static', filename='images/no-image.svg'))
    full_path = os.path.join(app.root_path, 'static', 'uploads',
                             os.path.basename(product.image_path))
    if not os.path.exists(full_path):
        return redirect(url_for('static', filename='images/no-image.svg'))
    return serve_watermarked_image(full_path)


@app.route('/secure-image/<token>')
def secure_image(token):
    """Serve images securely with signed tokens"""
    referer = request.headers.get('Referer', '')
    host = request.host_url.rstrip('/')
    if referer and not referer.startswith(host) and not referer.startswith('http://localhost'):
        abort(403)

    image_path = verify_image_token(token, max_age=app.config['IMAGE_TOKEN_EXPIRY'])
    if not image_path:
        abort(403)

    full_path = os.path.join(app.root_path, 'static', 'uploads', os.path.basename(image_path))
    if not os.path.exists(full_path):
        # Return placeholder
        return redirect(url_for('static', filename='images/no-image.svg'))

    return serve_watermarked_image(full_path)


# ─── AUTH ROUTES ─────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
@app.route('/admin/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        if current_user.is_admin:
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('my_orders'))
    if request.method == 'POST':
        identifier = request.form.get('username', '').strip()
        password   = request.form.get('password', '')
        # Allow login by username OR email
        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier)
        ).first()
        if user and user.check_password(password) and user.is_active:
            login_user(user, remember=request.form.get('remember') == '1')
            session.permanent = True
            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)
            if user.is_admin:
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('my_orders'))
        flash('Invalid username/email or password.', 'danger')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('my_orders'))
    if request.method == 'POST':
        username   = request.form.get('username', '').strip()
        email      = request.form.get('email', '').strip()
        full_name  = request.form.get('full_name', '').strip()
        phone      = request.form.get('phone', '').strip()
        password   = request.form.get('password', '')
        password2  = request.form.get('password2', '')

        if not username or not password:
            flash('Username and password are required.', 'danger')
        elif password != password2:
            flash('Passwords do not match.', 'danger')
        elif len(password) < 6:
            flash('Password must be at least 6 characters.', 'danger')
        elif User.query.filter_by(username=username).first():
            flash('Username already taken.', 'danger')
        elif email and User.query.filter_by(email=email).first():
            flash('Email already registered.', 'danger')
        else:
            user = User(
                username=username, email=email or None,
                full_name=full_name, phone=phone, role='Customer',
                address_line     = request.form.get('address_line','').strip() or None,
                address_city     = request.form.get('address_city','').strip() or None,
                address_province = request.form.get('address_province','').strip() or None,
                address_postal   = request.form.get('address_postal','').strip() or None,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            if user.has_address:
                flash(f'Welcome, {user.display_name}! Account created with your delivery address.', 'success')
            else:
                flash(f'Welcome, {user.display_name}! Set your delivery address in Profile for faster checkout.', 'info')
            return redirect(url_for('my_orders'))
    return render_template('register.html')


@app.route('/logout')
@app.route('/admin/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


# ─── CUSTOMER PORTAL ROUTES ───────────────────────────────────────────────────

@app.route('/my-orders')
@login_required
def my_orders():
    if current_user.is_admin:
        return redirect(url_for('admin_orders'))
    orders = Order.query.filter_by(user_id=current_user.id)                        .order_by(Order.created_at.desc()).all()
    return render_template('my_orders.html', orders=orders)


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    """Customer profile — save name, phone, address for auto-fill."""
    if current_user.is_admin:
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        action = request.form.get('action', 'info')

        if action == 'info':
            current_user.full_name = request.form.get('full_name', '').strip()
            current_user.phone     = request.form.get('phone', '').strip()
            email = request.form.get('email', '').strip()
            if email and email != current_user.email:
                existing = User.query.filter_by(email=email).first()
                if existing and existing.id != current_user.id:
                    flash('That email is already used by another account.', 'danger')
                    return redirect(url_for('profile'))
            current_user.email = email or None
            db.session.commit()
            flash('Profile updated!', 'success')

        elif action == 'address':
            current_user.address_line     = request.form.get('address_line', '').strip()
            current_user.address_city     = request.form.get('address_city', '').strip()
            current_user.address_province = request.form.get('address_province', '').strip()
            current_user.address_postal   = request.form.get('address_postal', '').strip()
            db.session.commit()
            flash('Delivery address saved!', 'success')

        elif action == 'password':
            current_pw  = request.form.get('current_password', '')
            new_pw      = request.form.get('new_password', '')
            confirm_pw  = request.form.get('confirm_password', '')
            if not current_user.check_password(current_pw):
                flash('Current password is incorrect.', 'danger')
            elif len(new_pw) < 6:
                flash('New password must be at least 6 characters.', 'danger')
            elif new_pw != confirm_pw:
                flash('New passwords do not match.', 'danger')
            else:
                current_user.set_password(new_pw)
                db.session.commit()
                flash('Password changed!', 'success')

        return redirect(url_for('profile'))

    recent_orders = Order.query.filter_by(user_id=current_user.id)                               .order_by(Order.created_at.desc()).limit(3).all()
    return render_template('profile.html', recent_orders=recent_orders)


@app.route('/my-orders/<order_number>')
@login_required
def my_order_detail(order_number):
    order = Order.query.filter_by(order_number=order_number).first_or_404()
    if not current_user.is_admin and order.user_id != current_user.id:
        abort(403)
    return render_template('my_order_detail.html', order=order)


@app.route('/my-orders/<order_number>/cancel', methods=['POST'])
@login_required
def cancel_order(order_number):
    """Allow customers to cancel their own Pending or Confirmed orders."""
    order = Order.query.filter_by(order_number=order_number).first_or_404()

    # Security: customers can only cancel their own orders
    if not current_user.is_admin and order.user_id != current_user.id:
        abort(403)

    # Only Pending or Confirmed orders can be cancelled by the customer
    if order.status not in ('Pending', 'Confirmed'):
        flash(f'This order cannot be cancelled — it is already {order.status}.', 'warning')
        return redirect(url_for('my_order_detail', order_number=order_number))

    reason = request.form.get('cancel_reason', '').strip() or 'Cancelled by customer'
    old_status = order.status
    order.status = 'Cancelled'

    # Restore stock for each item
    for item in order.items:
        if item.product:
            item.product.stock_quantity += item.quantity

    # Log the event
    event = OrderEvent(
        order_id = order.id,
        status   = 'Cancelled',
        actor    = current_user.display_name,
        note     = reason,
    )
    db.session.add(event)

    try:
        db.session.commit()
        log_automation('order_cancelled', 'success',
                       f'Order {order.order_number} cancelled by customer. Was: {old_status}.')
        flash('Your order has been cancelled and stock has been restored.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Could not cancel order: {e}', 'danger')

    return redirect(url_for('my_order_detail', order_number=order_number))


# ─── ADMIN ROUTES ─────────────────────────────────────────────────────────────
@app.route('/admin')
@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    total_products = Product.query.count()
    active_products = Product.query.filter_by(is_active=True).count()
    discounted = Product.query.filter(Product.discount_percentage > 0).count()
    low_stock = Product.query.filter(Product.stock_quantity <= Product.stock_threshold).count()
    active_campaigns = Campaign.query.filter_by(status='Active').count()
    fb_success = FacebookPostLog.query.filter_by(status='success').count()
    fb_failed = FacebookPostLog.query.filter_by(status='failed').count()

    # Category breakdown
    from sqlalchemy import func
    cat_data = db.session.query(Product.category, func.count(Product.id)).group_by(Product.category).all()
    recent_logs = AutomationLog.query.order_by(AutomationLog.timestamp.desc()).limit(10).all()
    recent_fb = FacebookPostLog.query.order_by(FacebookPostLog.created_at.desc()).limit(5).all()
    low_stock_products = Product.query.filter(
        Product.stock_quantity <= Product.stock_threshold,
        Product.is_active == True
    ).all()

    total_orders    = Order.query.count()
    pending_orders  = Order.query.filter_by(status='Pending').count()
    shipped_orders  = Order.query.filter_by(status='Shipped').count()
    recent_orders   = Order.query.order_by(Order.created_at.desc()).limit(5).all()

    return render_template('admin_dashboard.html',
                           total_products=total_products,
                           active_products=active_products,
                           discounted=discounted,
                           low_stock=low_stock,
                           active_campaigns=active_campaigns,
                           fb_success=fb_success,
                           fb_failed=fb_failed,
                           cat_data=cat_data,
                           recent_logs=recent_logs,
                           recent_fb=recent_fb,
                           low_stock_products=low_stock_products,
                           total_orders=total_orders,
                           pending_orders=pending_orders,
                           shipped_orders=shipped_orders,
                           recent_orders=recent_orders)


@app.route('/admin/products')
@login_required
def admin_products():
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    category = request.args.get('category', '')
    query = Product.query
    if search:
        query = query.filter(Product.name.contains(search))
    if category:
        query = query.filter(Product.category == category)
    products = query.order_by(Product.created_at.desc()).paginate(page=page, per_page=20, error_out=False)
    return render_template('admin_products.html', products=products, search=search, category=category)


@app.route('/admin/products/add', methods=['GET', 'POST'])
@login_required
def add_product():
    if request.method == 'POST':
        try:
            name = request.form['name'].strip()
            original_price = float(request.form['original_price'])
            discount_pct = float(request.form.get('discount_percentage', 0))

            product = Product(
                name=name,
                category=request.form['category'],
                crop_type=request.form.get('crop_type', ''),
                season_applicable=request.form.get('season_applicable', ''),
                original_price=original_price,
                discount_percentage=discount_pct,
                stock_quantity=int(request.form.get('stock_quantity', 0)),
                stock_threshold=int(request.form.get('stock_threshold', 10)),
                packaging_type=request.form.get('packaging_type', ''),
                description=request.form.get('description', ''),
                application_instructions=request.form.get('application_instructions', ''),
                safety_notes=request.form.get('safety_notes', ''),
                auto_post_enabled=bool(request.form.get('auto_post_enabled')),
                is_active=bool(request.form.get('is_active', True))
            )
            product.calculate_discounted_price()

            # Handle image upload
            if 'image' in request.files:
                file = request.files['image']
                if file and file.filename and allowed_file(file.filename):
                    filename = secure_filename(f"{int(time.time())}_{file.filename}")
                    upload_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    os.makedirs(os.path.dirname(upload_path), exist_ok=True)
                    file.save(upload_path)
                    add_watermark(upload_path)
                    product.image_path = filename

            db.session.add(product)
            db.session.commit()

            # Auto post if enabled and discounted — NON-BLOCKING thread
            if product.auto_post_enabled and product.discount_percentage > 0 and product.stock_quantity > 0:
                fb_post_async(product.id)

            log_automation('product_added', 'success', f'Product "{name}" added', product.id)
            flash(f'Product "{name}" added successfully!', 'success')
            return redirect(url_for('admin_products'))
        except Exception as e:
            flash(f'Error adding product: {str(e)}', 'danger')

    return render_template('admin_product_form.html', product=None, action='Add')


@app.route('/admin/products/edit/<int:product_id>', methods=['GET', 'POST'])
@login_required
def edit_product(product_id):
    product = Product.query.get_or_404(product_id)
    if request.method == 'POST':
        try:
            product.name = request.form['name'].strip()
            product.category = request.form['category']
            product.crop_type = request.form.get('crop_type', '')
            product.season_applicable = request.form.get('season_applicable', '')
            product.original_price = float(request.form['original_price'])
            product.discount_percentage = float(request.form.get('discount_percentage', 0))
            product.stock_quantity = int(request.form.get('stock_quantity', 0))
            product.stock_threshold = int(request.form.get('stock_threshold', 10))
            product.packaging_type = request.form.get('packaging_type', '')
            product.description = request.form.get('description', '')
            product.application_instructions = request.form.get('application_instructions', '')
            product.safety_notes = request.form.get('safety_notes', '')
            product.auto_post_enabled = bool(request.form.get('auto_post_enabled'))
            product.is_active = bool(request.form.get('is_active'))
            product.calculate_discounted_price()

            if 'image' in request.files:
                file = request.files['image']
                if file and file.filename and allowed_file(file.filename):
                    filename = secure_filename(f"{int(time.time())}_{file.filename}")
                    upload_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    os.makedirs(os.path.dirname(upload_path), exist_ok=True)
                    file.save(upload_path)
                    add_watermark(upload_path)
                    product.image_path = filename

            product.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.session.commit()

            # Auto-post if discount is set (or changed) and auto_post is enabled
            new_disc = product.discount_percentage or 0
            if product.auto_post_enabled and new_disc > 0 and product.stock_quantity > 0:
                fb_post_async(product.id)

            log_automation('product_updated', 'success', f'Product "{product.name}" updated', product.id)
            flash(f'Product updated!', 'success')
            return redirect(url_for('admin_products'))
        except Exception as e:
            flash(f'Error: {str(e)}', 'danger')

    return render_template('admin_product_form.html', product=product, action='Edit')


@app.route('/admin/products/delete/<int:product_id>', methods=['POST'])
@login_required
def delete_product(product_id):
    product = Product.query.get_or_404(product_id)
    name = product.name
    product.is_active = False
    db.session.commit()
    flash(f'Product "{name}" deactivated.', 'warning')
    return redirect(url_for('admin_products'))


@app.route('/admin/campaigns')
@login_required
def admin_campaigns():
    campaigns = Campaign.query.order_by(Campaign.created_at.desc()).all()
    return render_template('admin_campaigns.html', campaigns=campaigns)


@app.route('/admin/campaigns/add', methods=['GET', 'POST'])
@login_required
def add_campaign():
    if request.method == 'POST':
        try:
            start     = datetime.strptime(request.form['start_date'], '%Y-%m-%dT%H:%M')
            end       = datetime.strptime(request.form['end_date'],   '%Y-%m-%dT%H:%M')
            now_naive = datetime.now()   # local PHT
            # If the campaign window includes right now, make it Active immediately
            status    = 'Active' if start <= now_naive <= end else 'Scheduled'

            campaign = Campaign(
                name=request.form['name'],
                type=request.form['type'],
                start_date=start,
                end_date=end,
                discount_percentage=float(request.form['discount_percentage']),
                status=status,
                auto_post=bool(request.form.get('auto_post')),
                category_target=request.form.get('category_target', ''),
                crop_target=request.form.get('crop_target', '')
            )
            # Store custom caption if provided
            _cap_mode = request.form.get('caption_mode', 'auto')
            _custom_cap = request.form.get('custom_caption', '').strip()
            if _cap_mode == 'manual' and _custom_cap:
                try:
                    campaign.custom_caption = _custom_cap
                except Exception:
                    pass  # column may not exist yet — safe to ignore
            db.session.add(campaign)
            db.session.commit()

            post_count = 0
            if status == 'Active':
                # Apply discounts + fire FB posts right now
                q = Product.query.filter(Product.is_active == True)
                if campaign.category_target:
                    q = q.filter(Product.category == campaign.category_target)
                if campaign.crop_target:
                    q = q.filter(Product.crop_type == campaign.crop_target)
                products = q.all()
                for p in products:
                    p.discount_percentage = campaign.discount_percentage
                    p.calculate_discounted_price()
                    if campaign.auto_post and p.auto_post_enabled and p.stock_quantity > 0:
                        post_count += 1
                db.session.commit()
                # Post AFTER commit so product data is saved
                pids = [p.id for p in products
                        if campaign.auto_post and p.auto_post_enabled and p.stock_quantity > 0]
                # Resolve the caption to use: custom if manual mode, else None (auto-generate per product)
                campaign_caption = None
                if _cap_mode == 'manual' and _custom_cap:
                    campaign_caption = _custom_cap
                for pid in pids:
                    fb_post_async(pid, caption=campaign_caption, campaign_id=campaign.id)
                flash(f'Campaign "{campaign.name}" is ACTIVE! Posting {post_count} product(s) to Facebook...', 'success')
            else:
                flash(f'Campaign "{campaign.name}" scheduled for {start.strftime("%b %d %H:%M")}.', 'success')

            log_automation('campaign_created', 'success',
                           f'Campaign "{campaign.name}" created (status={status}, posts={post_count})',
                           campaign_id=campaign.id)
            return redirect(url_for('admin_campaigns'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error: {str(e)}', 'danger')
    return render_template('admin_campaign_form.html', campaign=None)


@app.route('/admin/campaigns/activate/<int:campaign_id>', methods=['POST'])
@login_required
def activate_campaign(campaign_id):
    """Manually activate any campaign and immediately post all matching products."""
    campaign = Campaign.query.get_or_404(campaign_id)
    # Determine if this is a re-post (campaign already Active) vs first activation
    is_repost = (campaign.status == 'Active')
    campaign.status = 'Active'
    q = Product.query.filter(Product.is_active == True)
    if campaign.category_target:
        q = q.filter(Product.category == campaign.category_target)
    if campaign.crop_target:
        q = q.filter(Product.crop_type == campaign.crop_target)
    products = q.all()
    pids = []
    for p in products:
        p.discount_percentage = campaign.discount_percentage
        p.calculate_discounted_price()
        # On a manual Re-Post the admin explicitly wants all matching products posted,
        # so bypass the auto_post / auto_post_enabled / stock guards.
        if is_repost:
            pids.append(p.id)
        elif campaign.auto_post and p.auto_post_enabled and p.stock_quantity > 0:
            pids.append(p.id)
    db.session.commit()
    for pid in pids:
        fb_post_async(pid, campaign_id=campaign.id)
    action_label = 'Re-Post' if is_repost else 'Activate'
    log_automation('campaign_activated', 'success',
                   f'Campaign "{campaign.name}" manually {action_label.lower()}d — {len(pids)} posts queued',
                   campaign_id=campaign.id)
    flash(f'Campaign "{campaign.name}" {action_label.lower()}d! Posting {len(pids)} product(s) to Facebook...', 'success')
    return redirect(url_for('admin_campaigns'))


@app.route('/admin/campaigns/delete/<int:campaign_id>', methods=['POST'])
@login_required
def delete_campaign(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    db.session.delete(campaign)
    db.session.commit()
    flash('Campaign deleted.', 'warning')
    return redirect(url_for('admin_campaigns'))


@app.route('/admin/logs')
@login_required
def admin_logs():
    page = request.args.get('page', 1, type=int)
    auto_logs = AutomationLog.query.order_by(AutomationLog.timestamp.desc()).paginate(page=page, per_page=30)
    fb_logs = FacebookPostLog.query.order_by(FacebookPostLog.created_at.desc()).limit(20).all()
    return render_template('admin_logs.html', auto_logs=auto_logs, fb_logs=fb_logs)


# ─── BULK IMPORT / EXPORT ─────────────────────────────────────────────────────

PRODUCT_EXPORT_COLUMNS = [
    'id', 'name', 'category', 'crop_type', 'season_applicable',
    'price', 'original_price', 'discount_percentage',
    'stock_quantity', 'stock_threshold', 'packaging_type',
    'description', 'application_instructions', 'safety_notes',
    'auto_post_enabled', 'is_active', 'post_tone',
]

@app.route('/admin/products/export')
@login_required
def export_products():
    """Export all products to Excel (.xlsx)."""
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    products = Product.query.order_by(Product.id).all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Products'

    # ── Header row ──
    header_fill = PatternFill('solid', fgColor='16A34A')
    header_font = Font(bold=True, color='FFFFFF', size=11)
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'),  bottom=Side(style='thin')
    )

    for col_idx, col_name in enumerate(PRODUCT_EXPORT_COLUMNS, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font       = header_font
        cell.fill       = header_fill
        cell.alignment  = Alignment(horizontal='center', vertical='center')
        cell.border     = thin_border

    # ── Data rows ──
    for row_idx, p in enumerate(products, 2):
        row_data = [
            p.id, p.name, p.category, p.crop_type or '', p.season_applicable or '',
            p.price, p.original_price, p.discount_percentage or 0,
            p.stock_quantity, p.stock_threshold, p.packaging_type or '',
            p.description or '', p.application_instructions or '', p.safety_notes or '',
            1 if p.auto_post_enabled else 0,
            1 if p.is_active else 0,
            p.post_tone or 'friendly',
        ]
        for col_idx, value in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border    = thin_border
            cell.alignment = Alignment(vertical='top', wrap_text=True)
        # Zebra stripe
        if row_idx % 2 == 0:
            for col_idx in range(1, len(PRODUCT_EXPORT_COLUMNS) + 1):
                ws.cell(row=row_idx, column=col_idx).fill = PatternFill('solid', fgColor='F0FDF4')

    # ── Column widths ──
    col_widths = [6, 30, 16, 18, 16, 10, 12, 10, 8, 8, 14, 40, 40, 30, 8, 8, 10]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 22

    # ── Instructions sheet ──
    ws2 = wb.create_sheet('Instructions')
    instructions = [
        ('AgriFortress — Bulk Product Import/Export Guide', None),
        ('', None),
        ('HOW TO IMPORT:', None),
        ('1. Fill in the Products sheet. You may delete the id column for new rows (leave blank = new product).', None),
        ('2. Required columns: name, category, price, original_price, stock_quantity', None),
        ('3. Boolean fields: use 1 for True, 0 for False (auto_post_enabled, is_active)', None),
        ('4. Existing id values → update that product. Blank id → create new product.', None),
        ('5. Upload via Admin → Products → Import Excel button.', None),
        ('', None),
        ('COLUMN REFERENCE:', None),
        ('id',                      'Leave blank to create new product; fill to update existing'),
        ('name',                    'Product name (required)'),
        ('category',                'e.g. Fertilizer, Pesticide, Seed, Equipment'),
        ('crop_type',               'e.g. Rice, Corn, Vegetables (optional)'),
        ('season_applicable',       'e.g. Wet Season, Dry Season (optional)'),
        ('price',                   'Current selling price (required)'),
        ('original_price',          'Base price before discounts (required)'),
        ('discount_percentage',     '0-100 (0 = no discount)'),
        ('stock_quantity',          'Current stock count (required)'),
        ('stock_threshold',         'Low-stock alert threshold (default 10)'),
        ('packaging_type',          'e.g. 1kg bag, 500ml bottle'),
        ('description',             'Product description'),
        ('application_instructions','How to use'),
        ('safety_notes',            'Safety / handling notes'),
        ('auto_post_enabled',       '1 = auto-post to Facebook when scheduled; 0 = manual only'),
        ('is_active',               '1 = visible in store; 0 = hidden'),
        ('post_tone',               'friendly | urgent | professional | lowstock'),
    ]
    ws2.column_dimensions['A'].width = 28
    ws2.column_dimensions['B'].width = 60
    for r, (a, b) in enumerate(instructions, 1):
        ws2.cell(row=r, column=1, value=a).font = Font(bold=(b is None and a != ''))
        if b is not None:
            ws2.cell(row=r, column=2, value=b)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    from datetime import datetime as _dt
    fname = f'agrifortress_products_{_dt.now().strftime("%Y%m%d_%H%M")}.xlsx'
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/admin/products/import', methods=['POST'])
@login_required
def import_products():
    """Import products from uploaded Excel (.xlsx) or CSV file."""
    import io, csv
    file = request.files.get('import_file')
    if not file or file.filename == '':
        flash('No file selected.', 'danger')
        return redirect(url_for('admin_products'))

    fname = secure_filename(file.filename).lower()
    created = updated = skipped = 0
    errors = []

    try:
        if fname.endswith('.xlsx') or fname.endswith('.xls'):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(file.read()), read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                flash('Empty spreadsheet.', 'danger')
                return redirect(url_for('admin_products'))
            headers = [str(h).strip().lower() if h else '' for h in rows[0]]
            data_rows = rows[1:]

        elif fname.endswith('.csv'):
            content = file.read().decode('utf-8-sig')
            reader  = csv.DictReader(io.StringIO(content))
            headers = [h.strip().lower() for h in reader.fieldnames or []]
            data_rows = [{h.strip().lower(): v for h, v in row.items()} for row in reader]
            # normalise to list-of-tuples style used below
            data_rows = [[row.get(h, '') for h in headers] for row in data_rows]
        else:
            flash('Unsupported file type. Please upload .xlsx or .csv', 'danger')
            return redirect(url_for('admin_products'))

        def _get(row, col, default=''):
            try:
                idx = headers.index(col)
                v = row[idx] if isinstance(row, (list, tuple)) else row.get(col, default)
                return v if v is not None else default
            except (ValueError, IndexError, AttributeError):
                return default

        def _bool(v):
            if isinstance(v, bool): return v
            return str(v).strip().lower() in ('1', 'true', 'yes')

        def _float(v, default=0.0):
            try:   return float(v)
            except: return default

        def _int(v, default=0):
            try:   return int(float(v))
            except: return default

        for row_num, row in enumerate(data_rows, 2):
            # skip fully empty rows
            if not any(str(c).strip() for c in (row if isinstance(row, (list, tuple)) else row.values())):
                continue

            name     = str(_get(row, 'name', '')).strip()
            category = str(_get(row, 'category', '')).strip()
            price    = _float(_get(row, 'price', 0))
            orig_price = _float(_get(row, 'original_price', price))

            if not name:
                errors.append(f'Row {row_num}: missing name — skipped')
                skipped += 1
                continue
            if not category:
                errors.append(f'Row {row_num}: missing category — skipped')
                skipped += 1
                continue

            # Determine create vs update
            raw_id = _get(row, 'id', '')
            product = None
            if raw_id and str(raw_id).strip() not in ('', 'None'):
                product = db.session.get(Product, _int(raw_id))

            if product is None:
                product = Product()
                is_new  = True
            else:
                is_new  = False

            product.name                    = name
            product.category                = category
            product.crop_type               = str(_get(row, 'crop_type', '')).strip() or None
            product.season_applicable       = str(_get(row, 'season_applicable', '')).strip() or None
            product.price                   = price
            product.original_price          = orig_price
            product.discount_percentage     = _float(_get(row, 'discount_percentage', 0))
            product.stock_quantity          = _int(_get(row, 'stock_quantity', 0))
            product.stock_threshold         = _int(_get(row, 'stock_threshold', 10))
            product.packaging_type          = str(_get(row, 'packaging_type', '')).strip() or None
            product.description             = str(_get(row, 'description', '')).strip() or None
            product.application_instructions= str(_get(row, 'application_instructions', '')).strip() or None
            product.safety_notes            = str(_get(row, 'safety_notes', '')).strip() or None
            product.auto_post_enabled       = _bool(_get(row, 'auto_post_enabled', False))
            product.is_active               = _bool(_get(row, 'is_active', True))
            product.post_tone               = str(_get(row, 'post_tone', 'friendly')).strip() or 'friendly'
            product.calculate_discounted_price()

            if is_new:
                db.session.add(product)
                created += 1
            else:
                updated += 1

        db.session.commit()
        log_automation('bulk_import', 'success',
                       f'Bulk import: {created} created, {updated} updated, {skipped} skipped')
        msg = f'✅ Import complete — {created} created, {updated} updated, {skipped} skipped.'
        if errors:
            msg += f' ⚠️ {len(errors)} row(s) had issues.'
        flash(msg, 'success' if not errors else 'warning')
        for e in errors[:5]:
            flash(e, 'warning')

    except Exception as ex:
        db.session.rollback()
        flash(f'Import failed: {ex}', 'danger')

    return redirect(url_for('admin_products'))


@app.route('/admin/products/import-template')
@login_required
def import_template():
    """Download a blank import template."""
    import io, openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Products'

    header_fill = PatternFill('solid', fgColor='16A34A')
    for col_idx, col in enumerate(PRODUCT_EXPORT_COLUMNS, 1):
        c = ws.cell(row=1, column=col_idx, value=col)
        c.font      = Font(bold=True, color='FFFFFF')
        c.fill      = header_fill
        c.alignment = Alignment(horizontal='center')

    # One sample row
    sample = ['', 'Sample Fertilizer', 'Fertilizer', 'Rice', 'Wet Season',
              250.00, 250.00, 0, 100, 10, '1kg bag',
              'High-quality fertilizer', 'Apply 2 bags per hectare', 'Keep away from children',
              0, 1, 'friendly']
    for col_idx, v in enumerate(sample, 1):
        ws.cell(row=2, column=col_idx, value=v)

    col_widths = [6, 30, 16, 18, 16, 10, 12, 10, 8, 8, 14, 40, 40, 30, 8, 8, 10]
    from openpyxl.utils import get_column_letter
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = 'A2'

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='agrifortress_import_template.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ─── BACKUP & RESTORE ─────────────────────────────────────────────────────────

@app.route('/admin/backup')
@login_required
def admin_backup():
    """Backup & Restore page."""
    import os as _os
    db_uri  = app.config['SQLALCHEMY_DATABASE_URI']
    db_path = db_uri.replace('sqlite:///', '')
    db_size = 0
    if db_path and not db_path.startswith(':') and _os.path.exists(db_path):
        db_size = _os.path.getsize(db_path)

    from sqlalchemy import text as _t
    counts = {}
    for tbl in ['products', 'orders', 'campaigns', 'users', 'automation_logs', 'facebook_post_logs']:
        try:
            counts[tbl] = db.session.execute(_t(f'SELECT COUNT(*) FROM {tbl}')).scalar()
        except Exception:
            counts[tbl] = '?'

    return render_template('admin_backup.html', db_size=db_size, counts=counts)


@app.route('/admin/backup/download')
@login_required
def backup_download():
    """Create and stream a full database + uploads backup as a ZIP."""
    import io, zipfile, json, os as _os
    from datetime import datetime as _dt
    from sqlalchemy import text as _t

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:

        # ── 1. SQLite DB file ──
        db_uri  = app.config['SQLALCHEMY_DATABASE_URI']
        db_path = db_uri.replace('sqlite:///', '')
        if db_path and not db_path.startswith(':') and _os.path.exists(db_path):
            zf.write(db_path, 'database/agristore.db')

        # ── 2. JSON data dump (human-readable, importable) ──
        tables = {
            'products':          Product,
            'campaigns':         Campaign,
            'users':             User,
            'automation_logs':   AutomationLog,
            'facebook_post_logs': FacebookPostLog,
        }
        dump = {}
        for tname, model in tables.items():
            rows = []
            for obj in model.query.all():
                row = {}
                for col in obj.__table__.columns:
                    val = getattr(obj, col.name)
                    if hasattr(val, 'isoformat'):
                        val = val.isoformat()
                    row[col.name] = val
                rows.append(row)
            dump[tname] = rows

        zf.writestr('data/full_dump.json', json.dumps(dump, indent=2, ensure_ascii=False))

        # ── 3. Products CSV (quick spreadsheet restore) ──
        import csv, io as _io
        csv_buf = _io.StringIO()
        writer  = csv.DictWriter(csv_buf, fieldnames=PRODUCT_EXPORT_COLUMNS)
        writer.writeheader()
        for p in Product.query.all():
            writer.writerow({
                'id': p.id, 'name': p.name, 'category': p.category,
                'crop_type': p.crop_type or '', 'season_applicable': p.season_applicable or '',
                'price': p.price, 'original_price': p.original_price,
                'discount_percentage': p.discount_percentage or 0,
                'stock_quantity': p.stock_quantity, 'stock_threshold': p.stock_threshold,
                'packaging_type': p.packaging_type or '',
                'description': p.description or '',
                'application_instructions': p.application_instructions or '',
                'safety_notes': p.safety_notes or '',
                'auto_post_enabled': 1 if p.auto_post_enabled else 0,
                'is_active': 1 if p.is_active else 0,
                'post_tone': p.post_tone or 'friendly',
            })
        zf.writestr('data/products.csv', csv_buf.getvalue())

        # ── 4. Product images ──
        uploads_dir = _os.path.join(app.root_path, 'static', 'uploads')
        if _os.path.isdir(uploads_dir):
            for fname in _os.listdir(uploads_dir):
                fpath = _os.path.join(uploads_dir, fname)
                if _os.path.isfile(fpath):
                    zf.write(fpath, f'uploads/{fname}')

        # ── 5. Backup manifest ──
        manifest = {
            'created_at': _dt.now().isoformat(),
            'version':    'agrifortress_v9',
            'db_tables':  {k: len(v) for k, v in dump.items()},
        }
        zf.writestr('manifest.json', json.dumps(manifest, indent=2))

    buf.seek(0)
    fname = f'agrifortress_backup_{_dt.now().strftime("%Y%m%d_%H%M%S")}.zip'
    log_automation('backup_created', 'success', f'Full backup downloaded: {fname}')
    return send_file(buf, as_attachment=True, download_name=fname, mimetype='application/zip')


@app.route('/admin/backup/restore', methods=['POST'])
@login_required
def backup_restore():
    """Restore from a backup ZIP created by backup_download."""
    import io, zipfile, json, os as _os, shutil

    file = request.files.get('restore_file')
    if not file or file.filename == '':
        flash('No file selected.', 'danger')
        return redirect(url_for('admin_backup'))

    mode = request.form.get('restore_mode', 'merge')  # merge | overwrite

    try:
        zf = zipfile.ZipFile(io.BytesIO(file.read()))
        names = zf.namelist()

        # ── Validate it's our backup ──
        if 'manifest.json' not in names:
            flash('Invalid backup file — missing manifest.json.', 'danger')
            return redirect(url_for('admin_backup'))

        manifest = json.loads(zf.read('manifest.json'))

        restored_products = restored_campaigns = 0

        def _coerce(model_cls, row):
            """Return a dict with datetime strings converted to datetime objects."""
            from datetime import datetime as _dt2
            result = {}
            for col in model_cls.__table__.columns:
                if col.name not in row:
                    continue
                val = row[col.name]
                if val is None:
                    result[col.name] = None
                    continue
                col_type = str(col.type.__class__.__name__).upper()
                if 'DATETIME' in col_type or 'DATE' in col_type:
                    if isinstance(val, str) and val:
                        for fmt in ('%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S',
                                    '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
                            try:
                                val = _dt2.strptime(val[:26], fmt); break
                            except ValueError:
                                pass
                result[col.name] = val
            return result

        # ── Restore from JSON dump ──
        if 'data/full_dump.json' in names:
            dump = json.loads(zf.read('data/full_dump.json'))

            if mode == 'overwrite':
                db.session.execute(__import__('sqlalchemy').text('DELETE FROM products'))
                db.session.execute(__import__('sqlalchemy').text('DELETE FROM campaigns'))
                db.session.commit()

            # ── Products ──
            for row in dump.get('products', []):
                pid = row.get('id')
                existing = db.session.get(Product, pid) if pid else None
                coerced  = _coerce(Product, row)
                if existing and mode == 'merge':
                    for k, v in coerced.items():
                        if k != 'id':
                            setattr(existing, k, v)
                elif not existing:
                    p = Product()
                    for k, v in coerced.items():
                        setattr(p, k, v)
                    db.session.add(p)
                    restored_products += 1
                else:
                    restored_products += 1

            # ── Campaigns ──
            for row in dump.get('campaigns', []):
                cid = row.get('id')
                existing = db.session.get(Campaign, cid) if cid else None
                coerced  = _coerce(Campaign, row)
                if existing and mode == 'merge':
                    for k, v in coerced.items():
                        if k != 'id':
                            setattr(existing, k, v)
                elif not existing:
                    c = Campaign()
                    for k, v in coerced.items():
                        setattr(c, k, v)
                    db.session.add(c)
                    restored_campaigns += 1

            db.session.commit()

        # ── Restore product images ──
        uploads_dir = _os.path.join(app.root_path, 'static', 'uploads')
        _os.makedirs(uploads_dir, exist_ok=True)
        img_count = 0
        for name in names:
            if name.startswith('uploads/') and not name.endswith('/'):
                fname = _os.path.basename(name)
                dest  = _os.path.join(uploads_dir, secure_filename(fname))
                if not _os.path.exists(dest) or mode == 'overwrite':
                    with zf.open(name) as src, open(dest, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                    img_count += 1

        log_automation('backup_restored', 'success',
                       f'Restore ({mode}): {restored_products} products, '
                       f'{restored_campaigns} campaigns, {img_count} images')
        flash(f'✅ Restore complete ({mode} mode) — '
              f'{restored_products} products, {restored_campaigns} campaigns, '
              f'{img_count} image(s) restored from backup dated '
              f'{manifest.get("created_at", "unknown")[:16]}.', 'success')

    except Exception as ex:
        db.session.rollback()
        flash(f'Restore failed: {ex}', 'danger')

    return redirect(url_for('admin_backup'))


# ─── API ROUTES ───────────────────────────────────────────────────────────────
@app.route('/api/caption/<int:product_id>')
@login_required
def api_caption(product_id):
    product = Product.query.get_or_404(product_id)
    tone = request.args.get('tone', 'friendly')
    caption = generate_ai_caption(product, tone)
    return jsonify({'caption': caption, 'tone': tone})


@app.route('/api/post-facebook/<int:product_id>', methods=['POST'])
@login_required
def api_post_facebook(product_id):
    product = Product.query.get_or_404(product_id)
    body    = request.get_json(silent=True) or {}
    caption = body.get('caption') or generate_ai_caption(product, body.get('tone', 'friendly'))
    success, result = post_to_facebook(product, caption)
    return jsonify({'success': success, 'result': result, 'caption': caption})


@app.route('/api/fb-post-now/<int:product_id>')
@login_required
def fb_post_now(product_id):
    """
    One-click manual post from the Products page.
    Queues an async Facebook post and redirects back with a flash message.
    """
    product = Product.query.get_or_404(product_id)
    tone    = request.args.get('tone', product.post_tone or 'friendly')
    if getattr(product, 'caption_mode', 'auto') == 'manual' and product.custom_caption:
        caption = product.custom_caption
    else:
        caption = generate_ai_caption(product, tone)
    _fb(f"Manual product-page trigger: '{product.name}' (id={product_id})")
    fb_post_async(product_id, caption=caption)
    log_automation('manual_post_triggered', 'success',
                   f'Manual trigger from Products page: "{product.name}"', product.id)
    flash(f'📤 Post queued for "{product.name}"! Check Facebook in a moment.', 'success')
    # Redirect back to the referring page (products list) or fall back to admin products
    return redirect(request.referrer or url_for('admin_products'))


@app.route('/api/scheduled-status')
@login_required
def api_scheduled_status():
    """Returns live status of all scheduled posts — used by admin UI for real-time refresh."""
    from sqlalchemy import text as _t

    def _to_dt(val):
        """Coerce DB value (datetime or string) to naive datetime, or None."""
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.replace(tzinfo=None) if val.tzinfo else val
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M'):
            try:
                return datetime.strptime(str(val)[:19], fmt)
            except ValueError:
                continue
        return None

    # Use LOCAL time — scheduled_post_at is stored as PHT (local), not UTC
    now = datetime.now()

    rows = db.session.execute(_t(
        "SELECT id, name, post_status, scheduled_post_at, last_posted_at, "
        "recurring_enabled, recurring_days, post_tone "
        "FROM products WHERE post_status IN ('scheduled','posted') AND is_active=1 "
        "ORDER BY scheduled_post_at ASC"
    )).fetchall()

    data = []
    for r in rows:
        sat     = _to_dt(r[3])
        lp      = _to_dt(r[4])
        overdue = (sat is not None) and (sat < now) and (r[2] == 'scheduled')
        data.append({
            'id':            r[0],
            'name':          r[1],
            'post_status':   r[2],
            'scheduled_at':  sat.isoformat() if sat else None,
            'last_posted':   lp.isoformat()  if lp  else None,
            'recurring':     bool(r[5]),
            'recurring_days': r[6],
            'tone':          r[7],
            'overdue':       overdue,
        })
    return jsonify({'now': now.isoformat(), 'posts': data})


@app.route('/api/force-check-scheduled', methods=['POST'])
@login_required
def api_force_check_scheduled():
    """Manually trigger check_scheduled_posts right now."""
    try:
        # Count before
        from sqlalchemy import text as _t
        now_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        before = db.session.execute(_t(
            "SELECT COUNT(*) FROM products WHERE post_status='scheduled' "
            "AND is_active=1 AND scheduled_post_at IS NOT NULL AND scheduled_post_at <= :now"
        ), {'now': now_ts}).fetchone()[0]

        check_scheduled_posts()

        # Count after
        still_due = db.session.execute(_t(
            "SELECT COUNT(*) FROM products WHERE post_status='scheduled' "
            "AND is_active=1 AND scheduled_post_at IS NOT NULL AND scheduled_post_at <= :now"
        ), {'now': now_ts}).fetchone()[0]

        fired = before - still_due
        if fired > 0:
            msg = f'✅ {fired} post(s) fired! Check Automation Logs for details.'
        elif before == 0:
            msg = 'No posts are currently due.'
        else:
            msg = f'⚠️ {before} post(s) found but could not be fired — check terminal logs.'

        return jsonify({'success': True, 'fired': fired, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'fired': 0, 'message': f'Error: {str(e)}'})
    from sqlalchemy import func
    cat_data = db.session.query(Product.category, func.count(Product.id)).group_by(Product.category).all()
    stock_data = db.session.query(Product.name, Product.stock_quantity).filter(
        Product.is_active == True
    ).order_by(Product.stock_quantity.asc()).limit(10).all()
    return jsonify({
        'categories': [{'label': c[0], 'value': c[1]} for c in cat_data],
        'stock': [{'label': s[0], 'value': s[1]} for s in stock_data]
    })


@app.route('/api/image-token/<int:product_id>')
def get_image_token(product_id):
    product = Product.query.get_or_404(product_id)
    if not product.image_path:
        return jsonify({'token': None})
    token = generate_image_token(product.image_path)
    return jsonify({'token': token, 'url': url_for('secure_image', token=token), 'expires': 90})



@app.route('/public-image/<int:product_id>')
def public_image(product_id):
    """Serve product image publicly (no watermark, no token) for the store index."""
    product = Product.query.get_or_404(product_id)
    if not product.image_path:
        return redirect(url_for('static', filename='images/no-image.svg'))
    full_path = os.path.join(app.root_path, 'static', 'uploads',
                             os.path.basename(product.image_path))
    if not os.path.exists(full_path):
        return redirect(url_for('static', filename='images/no-image.svg'))
    return send_file(full_path)
def set_lang(lang):
    if lang in ('en', 'fil'):
        session['lang'] = lang
    return redirect(request.referrer or url_for('index'))


@app.route('/manifest.json')
def pwa_manifest():
    return jsonify({
        "name": "AgriFortress Admin",
        "short_name": "AgriFortress",
        "start_url": "/admin",
        "display": "standalone",
        "background_color": "#111c12",
        "theme_color": "#16a34a",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    })


@app.route('/sw.js')
def service_worker():
    sw = """
self.addEventListener('install', e => { self.skipWaiting(); });
self.addEventListener('fetch',   e => {
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
"""
    return Response(sw, mimetype='application/javascript')




# ─── ADMIN — SCHEDULED POSTS ──────────────────────────────────────────────────
@app.route('/admin/scheduled')
@login_required
def admin_scheduled():
    from datetime import timedelta
    pht_offset = timedelta(hours=8)
    now        = datetime.now()

    from sqlalchemy import text as _sqlt
    ids = [r[0] for r in db.session.execute(_sqlt(
        "SELECT id FROM products \n"
        "WHERE post_status IN ('scheduled','posted') \n"
        "AND is_active=1 ORDER BY scheduled_post_at ASC"
    ))]
    scheduled = [p for p in [db.session.get(Product, i) for i in ids] if p]

    all_products = Product.query.filter_by(is_active=True).order_by(Product.name).all()
    all_campaigns = Campaign.query.filter(
        Campaign.status.in_(['Active', 'Scheduled'])
    ).order_by(Campaign.name).all()

    return render_template('admin_scheduled.html',
                           scheduled=scheduled,
                           all_products=all_products,
                           all_campaigns=all_campaigns,
                           pht_offset=pht_offset,
                           now=datetime.now())


@app.route('/admin/scheduled/set-campaign/<int:campaign_id>', methods=['POST'])
@login_required
def set_campaign_scheduled_post(campaign_id):
    """Schedule all products matching a campaign to post at a specific datetime."""
    from sqlalchemy import text as _text
    campaign = Campaign.query.get_or_404(campaign_id)

    scheduled_at_str = request.form.get('scheduled_post_at', '').strip()
    tone             = request.form.get('post_tone', 'friendly')
    recurring_enabled = bool(request.form.get('recurring_enabled'))
    recurring_days   = int(request.form.get('recurring_days', 7) or 7)

    try:
        scheduled_dt = datetime.strptime(scheduled_at_str, '%Y-%m-%dT%H:%M')
    except (ValueError, TypeError):
        flash('Invalid date/time format.', 'danger')
        return redirect(url_for('admin_scheduled'))

    # Find all products matching this campaign's targets
    q = Product.query.filter_by(is_active=True)
    if campaign.category_target:
        q = q.filter(Product.category == campaign.category_target)
    if campaign.crop_target:
        q = q.filter(Product.crop_type == campaign.crop_target)
    products = q.all()

    if not products:
        flash(f'No active products found matching campaign "{campaign.name}".', 'warning')
        return redirect(url_for('admin_scheduled'))

    count = 0
    try:
        for p in products:
            db.session.execute(_text(
                "UPDATE products SET "
                "post_status='scheduled', "
                "scheduled_post_at=:sat, "
                "post_tone=:tone, "
                "recurring_enabled=:rec, "
                "recurring_days=:days "
                "WHERE id=:id"
            ), {
                'sat':  scheduled_dt,
                'tone': tone,
                'rec':  1 if recurring_enabled else 0,
                'days': recurring_days,
                'id':   p.id
            })
            count += 1
        db.session.commit()
        log_automation('campaign_post_scheduled', 'success',
                       f'Campaign "{campaign.name}" — {count} products scheduled at {scheduled_dt.strftime("%b %d %H:%M")} PHT',
                       campaign_id=campaign.id)
        flash(f'✅ {count} product(s) from "{campaign.name}" scheduled for {scheduled_dt.strftime("%b %d, %Y %I:%M %p")} PHT', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')

    return redirect(url_for('admin_scheduled'))


@app.route('/admin/scheduled/set/<int:product_id>', methods=['POST'])
@login_required
def set_scheduled_post(product_id):
    from sqlalchemy import text as _text
    product = Product.query.get_or_404(product_id)

    scheduled_at_str  = request.form.get('scheduled_post_at', '').strip()
    tone              = request.form.get('post_tone', 'friendly')
    recurring_enabled = bool(request.form.get('recurring_enabled'))
    recurring_days    = int(request.form.get('recurring_days', 7) or 7)
    custom_caption    = request.form.get('custom_caption', '').strip()
    caption_mode      = request.form.get('caption_mode', 'auto')

    try:
        scheduled_dt = datetime.strptime(scheduled_at_str, '%Y-%m-%dT%H:%M')
    except (ValueError, TypeError):
        flash('Invalid date/time format.', 'danger')
        return redirect(url_for('admin_scheduled'))

    # Update via raw SQL to bypass any column cache issues
    try:
        db.session.execute(_text(
            "UPDATE products SET "
            "post_status='scheduled', "
            "scheduled_post_at=:sat, "
            "post_tone=:tone, "
            "recurring_enabled=:rec, "
            "recurring_days=:days, "
            "caption_mode=:cmode, "
            "custom_caption=:ccap "
            "WHERE id=:id"
        ), {
            'sat':   scheduled_dt,
            'tone':  tone,
            'rec':   1 if recurring_enabled else 0,
            'days':  recurring_days,
            'cmode': caption_mode,
            'ccap':  custom_caption if caption_mode == 'manual' else '',
            'id':    product.id
        })
        db.session.commit()
        log_automation('post_scheduled', 'success',
                       f'Post scheduled for "{product.name}" at {scheduled_dt.strftime("%b %d %H:%M")} PHT',
                       product.id)
        flash(f'✅ Post for "{product.name}" scheduled at {scheduled_dt.strftime("%b %d, %Y %I:%M %p")} PHT', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')

    return redirect(url_for('admin_scheduled'))


@app.route('/admin/scheduled/trigger/<int:product_id>', methods=['POST'])
@login_required
def trigger_scheduled_post(product_id):
    product = Product.query.get_or_404(product_id)
    tone    = getattr(product, 'post_tone', None) or 'friendly'
    if getattr(product, 'caption_mode', 'auto') == 'manual' and product.custom_caption:
        caption = product.custom_caption
    else:
        caption = generate_ai_caption(product, tone)
    fb_post_async(product_id, caption=caption)
    log_automation('manual_post_triggered', 'success',
                   f'Manual trigger: "{product.name}"', product.id)
    flash(f'📤 Post queued for "{product.name}"! Check Facebook in a moment.', 'success')
    return redirect(url_for('admin_scheduled'))


@app.route('/admin/scheduled/cancel/<int:product_id>', methods=['POST'])
@login_required
def cancel_scheduled_post(product_id):
    from sqlalchemy import text as _text
    product = Product.query.get_or_404(product_id)
    try:
        db.session.execute(_text(
            "UPDATE products SET post_status='none', "
            "scheduled_post_at=NULL, recurring_enabled=0 WHERE id=:id"
        ), {'id': product.id})
        db.session.commit()
        flash(f'Scheduled post for "{product.name}" cancelled.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('admin_scheduled'))

# ─── Error Handlers ───────────────────────────────────────────────────────────
@app.errorhandler(403)
def forbidden(e):
    return render_template('secure_error.html', code=403,
                           message='Access Forbidden', detail='You do not have permission to access this resource.'), 403

@app.errorhandler(404)
def not_found(e):
    return render_template('secure_error.html', code=404,
                           message='Page Not Found', detail='The page you are looking for does not exist.'), 404

@app.errorhandler(429)
def too_many_requests(e):
    return render_template('secure_error.html', code=429,
                           message='Too Many Requests', detail='Please slow down. You are making too many requests.'), 429


# ─── Init DB ──────────────────────────────────────────────────────────────────
def init_db():
    with app.app_context():
        db.create_all()
        # Create default admin
        admin = User.query.filter_by(username='admin').first()
        if not admin:
            admin = User(username='admin', role='Admin', full_name='AgriFortress Admin')
            admin.set_password('Admin@AgriF0rtress!')
            db.session.add(admin)
            db.session.commit()
            print("✅ Default admin created: admin / Admin@AgriF0rtress!")
        elif admin.role != 'Admin':
            admin.role = 'Admin'
            db.session.commit()
            print("✅ Existing admin role corrected to Admin")

        # Add sample products
        if Product.query.count() == 0:
            samples = [
                Product(name='Premium Hybrid Rice Seed OB-918', category='Seeds', crop_type='Rice',
                        season_applicable='Wet Season', original_price=580.0, discount_percentage=15,
                        stock_quantity=250, stock_threshold=20, packaging_type='5kg bag',
                        description='High-yielding hybrid rice seed with excellent lodging resistance.',
                        application_instructions='Soak seeds for 24 hrs before planting.',
                        safety_notes='Keep in cool dry place.', auto_post_enabled=True, is_active=True),
                Product(name='Urea Fertilizer 46-0-0', category='Fertilizer', crop_type='Corn',
                        season_applicable='All Season', original_price=1250.0, discount_percentage=10,
                        stock_quantity=180, stock_threshold=15, packaging_type='50kg sack',
                        description='High-nitrogen urea fertilizer for maximum vegetative growth.',
                        application_instructions='Apply 2 bags per hectare at 30 days after planting.',
                        safety_notes='Wear gloves and mask during application.', auto_post_enabled=True, is_active=True),
                Product(name='Complete Fertilizer 14-14-14', category='Fertilizer', crop_type='Vegetables',
                        season_applicable='All Season', original_price=1450.0, discount_percentage=0,
                        stock_quantity=95, stock_threshold=10, packaging_type='50kg sack',
                        description='Balanced NPK fertilizer for vegetables and root crops.',
                        application_instructions='Broadcast or side-dress at planting.',
                        safety_notes='Avoid contact with eyes.', auto_post_enabled=False, is_active=True),
                Product(name='Hand Tractor 7HP Diesel', category='Equipment', crop_type='Rice',
                        season_applicable='All Season', original_price=45000.0, discount_percentage=5,
                        stock_quantity=8, stock_threshold=2, packaging_type='Unit',
                        description='Powerful 7HP diesel hand tractor for land preparation.',
                        application_instructions='Use recommended diesel fuel. Change oil every 50 hours.',
                        safety_notes='Read manual before operation.', auto_post_enabled=True, is_active=True),
                Product(name='Corn Hybrid Seed DK-9133', category='Seeds', crop_type='Corn',
                        season_applicable='Dry Season', original_price=890.0, discount_percentage=20,
                        stock_quantity=7, stock_threshold=10, packaging_type='1kg bag',
                        description='Premium corn hybrid seed with 80-85 day maturity.',
                        application_instructions='Plant 2-3 seeds per hill, 75x25cm spacing.',
                        safety_notes='Treat seeds with fungicide before planting.', auto_post_enabled=True, is_active=True),
                Product(name='Knapsack Sprayer 16L', category='Tools', crop_type='Vegetables',
                        season_applicable='All Season', original_price=1800.0, discount_percentage=0,
                        stock_quantity=42, stock_threshold=5, packaging_type='Unit',
                        description='16-liter manual knapsack sprayer with adjustable nozzle.',
                        application_instructions='Fill tank 3/4 full for optimal pressure.',
                        safety_notes='Wear PPE when spraying pesticides.', auto_post_enabled=False, is_active=True),
            ]
            for p in samples:
                p.calculate_discounted_price()
                db.session.add(p)
            db.session.commit()
            print("✅ Sample products added.")

        # Add sample campaign
        if Campaign.query.count() == 0:
            camp = Campaign(
                name='Planting Season Kickoff 2024',
                type='Planting Season',
                start_date=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1),
                end_date=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30),
                discount_percentage=15,
                status='Active',
                auto_post=True,
                category_target='Seeds'
            )
            db.session.add(camp)
            db.session.commit()
            print("✅ Sample campaign added.")


# ─── ORDER ROUTES (PUBLIC) ────────────────────────────────────────────────────

@app.route('/order/<int:product_id>', methods=['GET', 'POST'])
@login_required
def place_order(product_id):
    """Order page — login required. Address auto-fills from profile."""
    if current_user.is_admin:
        flash('Admins cannot place orders.', 'warning')
        return redirect(url_for('index'))
    product = Product.query.get_or_404(product_id)
    if not product.is_active or product.stock_quantity < 1:
        abort(404)

    if request.method == 'POST':
        try:
            qty = int(request.form.get('quantity', 1))
            if qty < 1 or qty > product.stock_quantity:
                flash('Invalid quantity.', 'danger')
                return redirect(url_for('place_order', product_id=product_id))

            unit_price = product.discounted_price if product.discount_percentage > 0 else product.price
            subtotal   = round(unit_price * qty, 2)

            ship_method = 'J&T'
            ship_fee    = 100.0
            total     = round(subtotal + ship_fee, 2)

            # Address: use submitted form values, fall back to saved profile
            addr_line   = request.form.get('shipping_address','').strip() or (current_user.address_line or '')
            addr_city   = request.form.get('city','').strip()             or (current_user.address_city or '')
            addr_prov   = request.form.get('province','').strip()         or (current_user.address_province or '')
            addr_postal = request.form.get('postal_code','').strip()      or (current_user.address_postal or '')

            if not addr_line or not addr_city:
                flash('Delivery address is required. Please set it in your profile or fill it below.', 'danger')
                return redirect(url_for('place_order', product_id=product_id))

            # COD validation
            payment_method = request.form.get('payment_method', 'COD')
            _cod_check = _get_shipping_service().validate_cod(
                round(subtotal + 150.0, 2), payment_method)
            if not _cod_check['ok']:
                flash(_cod_check['reason'], 'warning')
                return redirect(url_for('place_order', product_id=product_id))

            order = Order(
                user_id          = current_user.id,
                customer_name    = request.form.get('customer_name','').strip() or current_user.display_name,
                customer_phone   = request.form.get('customer_phone','').strip() or (current_user.phone or ''),
                customer_email   = request.form.get('customer_email','').strip() or (current_user.email or ''),
                shipping_address = addr_line,
                city             = addr_city,
                province         = addr_prov,
                postal_code      = addr_postal,
                shipping_method  = ship_method,
                payment_method   = request.form.get('payment_method', 'COD'),
                subtotal         = subtotal,
                shipping_fee     = ship_fee,
                total_amount     = total,
                notes            = request.form.get('notes', '').strip(),
                status           = 'Pending',
                payment_status   = 'Unpaid',
            )
            order.generate_order_number()

            db.session.add(order)
            db.session.flush()  # get order.id

            item = OrderItem(
                order_id      = order.id,
                product_id    = product.id,
                product_name  = product.name,
                unit_price    = unit_price,
                quantity      = qty,
                subtotal      = subtotal,
                packaging_type = product.packaging_type or '',
            )
            db.session.add(item)

            # Initial timeline event
            event = OrderEvent(
                order_id   = order.id,
                status     = 'Pending',
                actor      = order.customer_name,
                note       = f'Order placed via {ship_method}. Payment: {order.payment_method}.',
            )
            db.session.add(event)

            # Deduct stock immediately (reserve)
            product.stock_quantity -= qty

            db.session.commit()

            log_automation('order_placed', 'success',
                           f'Order {order.order_number} placed for "{product.name}" x{qty}',
                           product_id=product.id)

            return redirect(url_for('order_confirmation', order_number=order.order_number))

        except Exception as e:
            db.session.rollback()
            flash(f'Error placing order: {e}', 'danger')
            return redirect(url_for('place_order', product_id=product_id))

    return render_template('order_form.html', product=product,
        gcash_number = app.config.get('GCASH_NUMBER', ''),
        gcash_name   = app.config.get('GCASH_NAME', ''),
        bank_name    = app.config.get('BANK_NAME', ''),
        bank_account = app.config.get('BANK_ACCOUNT', ''),
        bank_account_name = app.config.get('BANK_ACCOUNT_NAME', ''),
        payment_note = app.config.get('PAYMENT_REFERENCE_NOTE', ''),
    )


@app.route('/order/confirm/<order_number>')
def order_confirmation(order_number):
    order = Order.query.filter_by(order_number=order_number).first_or_404()
    return render_template('order_confirmation.html', order=order,
        gcash_number = app.config.get('GCASH_NUMBER', ''),
        gcash_name   = app.config.get('GCASH_NAME', ''),
        bank_name    = app.config.get('BANK_NAME', ''),
        bank_account = app.config.get('BANK_ACCOUNT', ''),
        bank_account_name = app.config.get('BANK_ACCOUNT_NAME', ''),
        payment_note = app.config.get('PAYMENT_REFERENCE_NOTE', ''),
    )


# ─── TRACKING HELPER ─────────────────────────────────────────────────────────

def track_jt_shipment(tracking_number: str):
    """
    Track a J&T Express PH shipment.
    Tries TrackingMore API first, then J&T Express direct.
    Returns (result_dict, source_string) or (None, None) on failure.
    """
    api_key = app.config.get('TRACKINGMORE_API_KEY', '').strip()

    # ── TrackingMore API ──────────────────────────────────────────────────────
    if api_key:
        try:
            headers = {
                'Tracking-Api-Key': api_key,
                'Content-Type': 'application/json',
            }
            # Register the tracking number (safe to call even if already registered)
            create_url = 'https://api.trackingmore.com/v4/trackings/create'
            requests.post(create_url,
                          json={'tracking_number': tracking_number, 'courier_code': 'jnt-ph'},
                          headers=headers, timeout=6)

            # Fetch tracking info
            get_url = (f'https://api.trackingmore.com/v4/trackings/get'
                       f'?tracking_numbers={tracking_number}&courier_code=jnt-ph')
            resp = requests.get(get_url, headers=headers, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                items = (data.get('data') or {}).get('items') or data.get('data', [])
                if isinstance(items, dict):
                    items = list(items.values())
                if items:
                    t = items[0]
                    raw_cps = (t.get('origin_info') or {}).get('trackinfo', []) or \
                              (t.get('destination_info') or {}).get('trackinfo', []) or []
                    checkpoints = []
                    for cp in raw_cps:
                        if isinstance(cp, dict):
                            checkpoints.append({
                                'tracking_detail': cp.get('StatusDescription')
                                                   or cp.get('tracking_detail')
                                                   or cp.get('status', ''),
                                'location':        cp.get('Details') or cp.get('location', ''),
                                'checkpoint_time': cp.get('Date') or cp.get('checkpoint_time', ''),
                            })
                    return {
                        'tracking_number':    t.get('tracking_number', tracking_number),
                        'carrier':            'J&T Express PH',
                        'status':             t.get('delivery_status') or t.get('status', 'In Transit'),
                        'latest':             t.get('latest_event', ''),
                        'estimated_delivery': t.get('expected_delivery', ''),
                        'checkpoints':        checkpoints,
                    }, 'trackingmore'
        except Exception as e:
            log.warning('TrackingMore API error: %s', e)

    # ── J&T Express PH public tracking fallback ───────────────────────────────
    try:
        resp = requests.post(
            'https://www.jtexpress.ph/index/query/gateResultList.json',
            data={'billCodes': tracking_number, 'language': 'en'},
            headers={'Content-Type': 'application/x-www-form-urlencoded',
                     'Referer': 'https://www.jtexpress.ph/',
                     'User-Agent': 'Mozilla/5.0'},
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            items = data.get('data') or []
            details = (items[0].get('details') or []) if items else []
            if details:
                checkpoints = [{
                    'tracking_detail': d.get('process', d.get('desc', '')),
                    'location':        d.get('city', ''),
                    'checkpoint_time': f"{d.get('date', '')} {d.get('time', '')}".strip(),
                } for d in details]
                return {
                    'tracking_number': tracking_number,
                    'carrier':         'J&T Express PH',
                    'status':          checkpoints[0].get('tracking_detail', 'In Transit'),
                    'latest':          checkpoints[0].get('tracking_detail', ''),
                    'estimated_delivery': '',
                    'checkpoints':     checkpoints,
                }, 'jnt_direct'
    except Exception as e:
        log.warning('J&T direct tracking error: %s', e)

    return None, None


@app.route('/order/track', methods=['GET', 'POST'])
def track_order():
    """Public order tracking page using MSC Shipping free track API."""
    result = None
    error  = None
    tracking_no = ''

    if request.method == 'POST':
        tracking_no = request.form.get('tracking_no', '').strip()
        if tracking_no:
            # First check internal DB
            order = Order.query.filter(
                (Order.tracking_number == tracking_no) |
                (Order.order_number == tracking_no)
            ).first()

            if order:
                result = {
                    'source': 'internal',
                    'order': order,
                }
            else:
                # Try J&T Express PH via TrackingMore API first, then J&T direct
                try:
                    jt_result, jt_source = track_jt_shipment(tracking_no)
                    if jt_result:
                        result = {
                            'source':     'jt_api',
                            'data':       jt_result,
                            'api_source': jt_source,
                        }
                    else:
                        # Both APIs failed — provide direct J&T link
                        result = {
                            'source':      'manual',
                            'manual_url':  f'https://www.jtexpress.ph/track-and-trace?query={tracking_no}',
                            'tracking_no': tracking_no,
                        }
                        error = 'Live tracking unavailable. Use the J&T Express link below to track on their website.'
                except Exception as e:
                    error = f'Could not reach tracking service. Please try again later.'
                    result = {
                        'source':     'manual',
                        'manual_url': f'https://www.jtexpress.ph/track-and-trace?query={tracking_no}',
                        'tracking_no': tracking_no,
                    }

    return render_template('order_track.html', result=result, error=error, tracking_no=tracking_no)


# ─── ORDER ADMIN ROUTES ───────────────────────────────────────────────────────

@app.route('/admin/orders')
@login_required
def admin_orders():
    page    = request.args.get('page', 1, type=int)
    status  = request.args.get('status', '')
    search  = request.args.get('search', '')
    query   = Order.query
    if status:
        query = query.filter(Order.status == status)
    if search:
        query = query.filter(
            Order.order_number.contains(search) |
            Order.customer_name.contains(search) |
            Order.customer_phone.contains(search)
        )
    orders = query.order_by(Order.created_at.desc()).paginate(page=page, per_page=20, error_out=False)
    status_counts = {
        'Pending':    Order.query.filter_by(status='Pending').count(),
        'Confirmed':  Order.query.filter_by(status='Confirmed').count(),
        'Processing': Order.query.filter_by(status='Processing').count(),
        'Shipped':    Order.query.filter_by(status='Shipped').count(),
        'Delivered':  Order.query.filter_by(status='Delivered').count(),
        'Cancelled':  Order.query.filter_by(status='Cancelled').count(),
    }
    return render_template('admin_orders.html', orders=orders,
                           status=status, search=search, status_counts=status_counts)


@app.route('/admin/orders/<int:order_id>')
@login_required
def admin_order_detail(order_id):
    order = Order.query.get_or_404(order_id)
    return render_template('admin_order_detail.html', order=order)


@app.route('/admin/orders/<int:order_id>/update-status', methods=['POST'])
@login_required
def update_order_status(order_id):
    order = Order.query.get_or_404(order_id)
    new_status = request.form.get('status')
    tracking   = request.form.get('tracking_number', '').strip()
    payment_status = request.form.get('payment_status', '').strip()

    valid_statuses = ['Pending', 'Confirmed', 'Processing', 'Shipped', 'Delivered', 'Cancelled']
    admin_note = request.form.get('admin_note', '').strip()

    if new_status in valid_statuses:
        old_status = order.status
        # If cancelling, restore stock
        if new_status == 'Cancelled' and old_status != 'Cancelled':
            for item in order.items:
                prod = Product.query.get(item.product_id)
                if prod:
                    prod.stock_quantity += item.quantity

        order.status = new_status
        if tracking:
            order.tracking_number = tracking
        if payment_status:
            order.payment_status = payment_status

        # Log the status change as an OrderEvent
        if new_status != old_status or admin_note:
            note_text = admin_note if admin_note else f'Status changed from {old_status} to {new_status}.'
            if tracking and new_status == 'Shipped':
                note_text += f' Tracking: {tracking}.'
            event = OrderEvent(
                order_id = order.id,
                status   = new_status,
                actor    = f'Admin ({current_user.username})',
                note     = note_text,
            )
            db.session.add(event)

        db.session.commit()
        flash(f'Order {order.order_number} updated to {new_status}.', 'success')
    else:
        flash('Invalid status.', 'danger')

    return redirect(url_for('admin_order_detail', order_id=order_id))


@app.route('/admin/inventory')
@login_required
def admin_inventory():
    """Inventory overview with order-aware stock management."""
    from sqlalchemy import func
    products = Product.query.order_by(Product.stock_quantity.asc()).all()

    # Order stats per product
    sold_map = {}
    rows = db.session.query(
        OrderItem.product_id,
        func.sum(OrderItem.quantity)
    ).join(Order).filter(
        Order.status.notin_(['Cancelled'])
    ).group_by(OrderItem.product_id).all()
    for pid, total_sold in rows:
        sold_map[pid] = total_sold or 0

    pending_map = {}
    prows = db.session.query(
        OrderItem.product_id,
        func.sum(OrderItem.quantity)
    ).join(Order).filter(
        Order.status.in_(['Pending', 'Confirmed', 'Processing'])
    ).group_by(OrderItem.product_id).all()
    for pid, total_pending in prows:
        pending_map[pid] = total_pending or 0

    total_value = sum((p.price or 0) * (p.stock_quantity or 0) for p in products)
    low_stock_count = sum(1 for p in products if p.is_low_stock and p.is_active)

    return render_template('admin_inventory.html',
                           products=products,
                           sold_map=sold_map,
                           pending_map=pending_map,
                           total_value=total_value,
                           low_stock_count=low_stock_count)


@app.route('/admin/inventory/adjust/<int:product_id>', methods=['POST'])
@login_required
def adjust_stock(product_id):
    product = Product.query.get_or_404(product_id)
    action  = request.form.get('action')   # add | set
    amount  = int(request.form.get('amount', 0))

    if action == 'add':
        product.stock_quantity = max(0, product.stock_quantity + amount)
        flash(f'Added {amount} units to {product.name}. New stock: {product.stock_quantity}', 'success')
    elif action == 'set':
        product.stock_quantity = max(0, amount)
        flash(f'Stock for {product.name} set to {product.stock_quantity}.', 'success')

    db.session.commit()
    return redirect(url_for('admin_inventory'))


# ─── MSC Shipping Rate API ────────────────────────────────────────────────────

# ─── Shipping Service API Routes ─────────────────────────────────────────────

@app.route('/api/shipping-rates', methods=['POST'])
def api_get_rates():
    data = request.get_json() or {}
    return jsonify(_get_shipping_service().get_rates(data))


@app.route('/api/shipping-rate')
def shipping_rate():
    method = request.args.get('method', 'J&T')
    svc    = _get_shipping_service()
    result = svc.get_rates({'weight_kg': float(request.args.get('weight', 0.5))})
    for r in result.get('rates', []):
        if method.lower() in r['courier'].lower():
            return jsonify(r)
    FLAT = {'J&T': 100}
    return jsonify({'fee': 100, 'days': '2-3',
                    'courier': 'J&T', 'provider': 'fallback'})


@app.route('/api/jt-track')
@app.route('/api/msc-track')
def jt_track_api():
    """Track shipment — Shippo-first if label purchased there, else JNT → TM → web."""
    tn            = request.args.get('tracking', '').strip()
    courier       = request.args.get('courier', 'J&T').strip()
    provider_hint = request.args.get('provider', '').strip()
    if not tn:
        return jsonify({'error': 'No tracking number provided'}), 400
    # Auto-detect provider from DB Shipment record
    if not provider_hint:
        ship = Shipment.query.filter_by(tracking_number=tn).first()
        if ship:
            provider_hint = ship.provider or ''
    svc    = _get_shipping_service()
    result = svc.track(tn, courier=courier, provider_hint=provider_hint)
    return jsonify(result), (200 if result.get('success') else 404)


@app.route('/api/create-shipment', methods=['POST'])
@login_required
def api_create_shipment():
    """Book shipment via Shippo → EasyPost → manual fallback."""
    data     = request.get_json() or {}
    order_id = data.get('order_id')
    if not order_id:
        return jsonify({'error': 'order_id required'}), 400
    order = Order.query.get_or_404(order_id)
    result = _get_shipping_service().create_shipment({
        'order_number':   order.order_number,
        'customer_name':  order.customer_name,
        'shipping_address': order.shipping_address,
        'city':           order.city or '',
        'province':       order.province or '',
        'postal_code':    order.postal_code or '',
        'customer_phone': order.customer_phone,
        'customer_email': order.customer_email or '',
        'shipping_method': order.shipping_method,
        'weight_kg':      float(data.get('weight_kg', 0.5)),
        'total_amount':   order.total_amount,
        'product_name':   ', '.join(i.product_name for i in order.items),
        'payment_method': order.payment_method,
    })
    if result.get('success') and result.get('tracking_number'):
        ship = Shipment(
            order_id        = order.id,
            provider        = result['provider'],
            tracking_number = result['tracking_number'],
            label_url       = result.get('label_url', ''),
            shipment_id     = result.get('shipment_id', ''),
            courier         = order.shipping_method,
            fee             = result.get('rate', {}).get('fee', 0) if result.get('rate') else 0,
            status          = 'shipped',
        )
        db.session.add(ship)
        order.tracking_number = result['tracking_number']
        if order.status == 'Pending':
            order.status = 'Confirmed'
            ev = OrderEvent(
                order_id = order.id,
                status   = 'Confirmed',
                actor    = f'Admin ({current_user.username})',
                note     = f'Shipment booked via {result["provider"]}. Tracking: {result["tracking_number"]}',
            )
            db.session.add(ev)
        db.session.commit()
    return jsonify(result)


@app.route('/api/provider-status')
@login_required
def api_provider_status():
    """Health check — which providers are configured and active."""
    return jsonify({'providers': _get_shipping_service().get_provider_status()})


@app.route('/api/validate-address', methods=['POST'])
@login_required
def api_validate_address():
    """Validate a shipping address via Shippo."""
    data   = request.get_json() or {}
    result = _get_shipping_service().validate_address(data)
    return jsonify(result)


@app.route('/api/cancel-shipment', methods=['POST'])
@login_required
def api_cancel_shipment():
    """Cancel a J&T shipment before pickup."""
    data     = request.get_json() or {}
    tn       = data.get('tracking_number', '').strip()
    order_no = data.get('order_number', '').strip()
    if not tn:
        return jsonify({'error': 'tracking_number required'}), 400
    result = _get_shipping_service().cancel_shipment(tn, order_no)
    if result.get('success'):
        shipment = Shipment.query.filter_by(tracking_number=tn).first()
        if shipment:
            shipment.status = 'cancelled'
            db.session.commit()
    return jsonify(result), (200 if result.get('success') else 400)


@app.route('/api/shippo-webhook', methods=['POST'])
def shippo_webhook():
    """
    Shippo webhook — receives push tracking updates automatically.
    Setup: app.goshippo.com → Settings → Webhooks
    URL:   https://yourdomain.com/api/shippo-webhook
    Event: track_updated
    """
    try:
        payload = request.get_json(force=True) or {}
        event   = payload.get('event', '')
        if event == 'track_updated':
            data       = payload.get('data', {})
            tn         = data.get('tracking_number', '')
            ts         = data.get('tracking_status', {}) or {}
            new_status = ts.get('status', '')
            if tn and new_status:
                from services.shipping.providers.shippo_provider import ShippoProvider
                mapped = ShippoProvider._map_status(new_status)
                ship   = Shipment.query.filter_by(tracking_number=tn).first()
                if ship:
                    ship.status     = mapped
                    ship.updated_at = datetime.utcnow()
                    loc = ts.get('location', {}) or {}
                    tlog = TrackingLog(
                        shipment_id = ship.id,
                        provider    = 'shippo_webhook',
                        status      = mapped,
                        event       = ts.get('status_details', ''),
                        location    = loc.get('city','') if isinstance(loc,dict) else '',
                        timestamp   = ts.get('status_date', ''),
                    )
                    db.session.add(tlog)
                    order = Order.query.get(ship.order_id)
                    if order:
                        status_map = {
                            'shipped': 'Shipped', 'in_transit': 'Shipped',
                            'out_for_delivery': 'Shipped', 'delivered': 'Delivered',
                            'failed': 'Cancelled',
                        }
                        new_ord = status_map.get(mapped)
                        if new_ord and order.status not in ('Delivered','Cancelled'):
                            order.status = new_ord
                            db.session.add(OrderEvent(
                                order_id = order.id, status = new_ord,
                                actor    = 'Shippo Webhook',
                                note     = ts.get('status_details', f'Auto: {new_status}'),
                            ))
                    db.session.commit()
        return jsonify({'received': True}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── COD Routes ────────────────────────────────────────────────────────────────

@app.route('/admin/cod')
@login_required
def admin_cod():
    cod_orders = Order.query.filter_by(payment_method='COD').order_by(Order.created_at.desc()).all()
    summary = _get_shipping_service().cod_summary([{
        'payment_method': o.payment_method,
        'total_amount':   o.total_amount,
        'cod_status':     (o.cod_transaction.cod_status if o.cod_transaction else 'pending'),
    } for o in cod_orders])
    return render_template('admin_cod.html', cod_orders=cod_orders, summary=summary)


@app.route('/admin/cod/<int:order_id>/update', methods=['POST'])
@login_required
def update_cod_status(order_id):
    order      = Order.query.get_or_404(order_id)
    new_status = request.form.get('cod_status')
    if not order.cod_transaction:
        ct = CODTransaction(order_id=order.id, cod_amount=order.total_amount, cod_status='pending')
        db.session.add(ct)
        db.session.flush()
        db.session.refresh(order)
    ct = order.cod_transaction
    if ct is None:
        flash('Could not find or create COD record.', 'danger')
        return redirect(url_for('admin_cod'))
    if new_status == 'collected':
        ct.cod_status = 'collected'; ct.collected_at = datetime.utcnow()
    elif new_status == 'remitted':
        ct.cod_status = 'remitted';  ct.remitted_at  = datetime.utcnow()
    elif new_status == 'failed':
        ct.cod_status = 'failed'
    db.session.commit()
    flash(f'COD status updated to {new_status}.', 'success')
    return redirect(url_for('admin_cod'))


if __name__ == '__main__':
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs('instance', exist_ok=True)
    init_db()
    # line 2308 — change 5000 to something outside the excluded range
    app.run(debug=True, host='0.0.0.0', port=8080) 
    