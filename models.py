from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email         = db.Column(db.String(200), unique=True, nullable=True, index=True)
    full_name     = db.Column(db.String(200))
    phone         = db.Column(db.String(50))
    password_hash = db.Column(db.String(256), nullable=False)
    role          = db.Column(db.String(20), default='Customer')  # Admin | Customer
    is_active     = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    # ── Saved address profile (auto-fills order form) ──────────────────
    address_line  = db.Column(db.Text)          # Street / Barangay
    address_city  = db.Column(db.String(100))   # City / Municipality
    address_province = db.Column(db.String(100))
    address_postal   = db.Column(db.String(20))

    orders = db.relationship('Order', backref='customer', lazy=True)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def is_admin(self):
        return self.role == 'Admin'

    @property
    def display_name(self):
        return self.full_name or self.username

    @property
    def has_address(self):
        return bool(self.address_line and self.address_city)

    def __repr__(self):
        return f'<User {self.username} ({self.role})>'


class Product(db.Model):
    __tablename__ = 'products'
    id                  = db.Column(db.Integer, primary_key=True)
    name                = db.Column(db.String(200), nullable=False, index=True)
    category            = db.Column(db.String(50),  nullable=False, index=True)
    crop_type           = db.Column(db.String(100), index=True)
    season_applicable   = db.Column(db.String(100))
    price               = db.Column(db.Float, nullable=False)
    original_price      = db.Column(db.Float, nullable=False)
    discount_percentage = db.Column(db.Float, default=0.0)
    discounted_price    = db.Column(db.Float)
    stock_quantity      = db.Column(db.Integer, default=0)
    stock_threshold     = db.Column(db.Integer, default=10)
    packaging_type      = db.Column(db.String(100))
    description         = db.Column(db.Text)
    application_instructions = db.Column(db.Text)
    safety_notes        = db.Column(db.Text)
    image_path          = db.Column(db.String(500))
    auto_post_enabled   = db.Column(db.Boolean, default=False)
    is_active           = db.Column(db.Boolean, default=True, index=True)
    post_status         = db.Column(db.String(20), default='none')
    post_tone           = db.Column(db.String(20), default='friendly')
    scheduled_post_at   = db.Column(db.DateTime, nullable=True)
    recurring_enabled   = db.Column(db.Boolean, default=False)
    recurring_days      = db.Column(db.Integer, default=7)
    last_posted_at      = db.Column(db.DateTime, nullable=True)
    caption_mode        = db.Column(db.String(10), default='auto')
    custom_caption      = db.Column(db.Text, nullable=True)
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at          = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    facebook_posts  = db.relationship('FacebookPostLog', backref='product', lazy=True)
    automation_logs = db.relationship('AutomationLog',   backref='product', lazy=True)
    order_items     = db.relationship('OrderItem',        backref='product', lazy=True)

    def calculate_discounted_price(self):
        if self.discount_percentage and self.discount_percentage > 0:
            self.discounted_price = round(self.original_price * (1 - self.discount_percentage / 100), 2)
            self.price = self.discounted_price
        else:
            self.discounted_price = self.original_price
            self.price = self.original_price

    @property
    def is_low_stock(self):
        return self.stock_quantity <= self.stock_threshold

    @property
    def is_discounted(self):
        return bool(self.discount_percentage and self.discount_percentage > 0)

    def __repr__(self):
        return f'<Product {self.name}>'


class Order(db.Model):
    __tablename__ = 'orders'
    id           = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(20), unique=True, nullable=False, index=True)

    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)

    customer_name    = db.Column(db.String(200), nullable=False)
    customer_phone   = db.Column(db.String(50),  nullable=False)
    customer_email   = db.Column(db.String(200))
    shipping_address = db.Column(db.Text, nullable=False)
    city             = db.Column(db.String(100))
    province         = db.Column(db.String(100))
    postal_code      = db.Column(db.String(20))

    shipping_method = db.Column(db.String(50), default='MSC')
    tracking_number = db.Column(db.String(200))
    msc_booking_ref = db.Column(db.String(200))

    status         = db.Column(db.String(30), default='Pending')
    subtotal       = db.Column(db.Float, default=0.0)
    shipping_fee   = db.Column(db.Float, default=0.0)
    total_amount   = db.Column(db.Float, default=0.0)
    payment_method = db.Column(db.String(50), default='COD')
    payment_status = db.Column(db.String(20), default='Unpaid')

    notes      = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    items  = db.relationship('OrderItem',  backref='order', lazy=True, cascade='all, delete-orphan')
    events = db.relationship('OrderEvent', backref='order', lazy=True,
                             cascade='all, delete-orphan', order_by='OrderEvent.created_at')

    def generate_order_number(self):
        import random, string
        date_part = datetime.utcnow().strftime('%y%m%d')
        rand_part = ''.join(random.choices(string.digits, k=4))
        self.order_number = f'AF{date_part}{rand_part}'

    @property
    def item_count(self):
        return sum(i.quantity for i in self.items)

    def __repr__(self):
        return f'<Order {self.order_number}>'


class OrderItem(db.Model):
    __tablename__ = 'order_items'
    id            = db.Column(db.Integer, primary_key=True)
    order_id      = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    product_id    = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    product_name  = db.Column(db.String(200), nullable=False)
    unit_price    = db.Column(db.Float, nullable=False)
    quantity      = db.Column(db.Integer, nullable=False, default=1)
    subtotal      = db.Column(db.Float, nullable=False)
    packaging_type = db.Column(db.String(100))

    def __repr__(self):
        return f'<OrderItem {self.product_name} x{self.quantity}>'


class OrderEvent(db.Model):
    __tablename__ = 'order_events'
    id         = db.Column(db.Integer, primary_key=True)
    order_id   = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False, index=True)
    status     = db.Column(db.String(30), nullable=False)
    actor      = db.Column(db.String(100))
    note       = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<OrderEvent {self.status} @ {self.created_at}>'


class Campaign(db.Model):
    __tablename__ = 'campaigns'
    id                  = db.Column(db.Integer, primary_key=True)
    name                = db.Column(db.String(200), nullable=False)
    type                = db.Column(db.String(50))
    start_date          = db.Column(db.DateTime, nullable=False)
    end_date            = db.Column(db.DateTime, nullable=False)
    discount_percentage = db.Column(db.Float, nullable=False)
    status              = db.Column(db.String(20), default='Scheduled')
    auto_post           = db.Column(db.Boolean, default=True)
    category_target     = db.Column(db.String(50))
    crop_target         = db.Column(db.String(100))
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<Campaign {self.name}>'


class FacebookPostLog(db.Model):
    __tablename__ = 'facebook_post_logs'
    id               = db.Column(db.Integer, primary_key=True)
    product_id       = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    campaign_id      = db.Column(db.Integer, db.ForeignKey('campaigns.id'), nullable=True)
    post_id          = db.Column(db.String(200))
    status           = db.Column(db.String(20))
    caption          = db.Column(db.Text)
    response_message = db.Column(db.Text)
    retry_count      = db.Column(db.Integer, default=0)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<FacebookPostLog {self.id} - {self.status}>'


class AutomationLog(db.Model):
    __tablename__ = 'automation_logs'
    id          = db.Column(db.Integer, primary_key=True)
    event_type  = db.Column(db.String(100))
    product_id  = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaigns.id'), nullable=True)
    status      = db.Column(db.String(20))
    message     = db.Column(db.Text)
    timestamp   = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<AutomationLog {self.event_type} - {self.timestamp}>'


# ─── Shipping System Tables (v10) ─────────────────────────────────────────────

class Shipment(db.Model):
    """One shipment per order — stores provider result and label."""
    __tablename__ = 'shipments'
    id             = db.Column(db.Integer, primary_key=True)
    order_id       = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False, index=True)
    provider       = db.Column(db.String(50))    # shippo | easypost | jnt | lbc | manual
    tracking_number= db.Column(db.String(200), index=True)
    shipment_id    = db.Column(db.String(200))   # provider-internal ID
    label_url      = db.Column(db.Text)
    courier        = db.Column(db.String(100))
    service        = db.Column(db.String(100))
    fee            = db.Column(db.Float, default=0.0)
    status         = db.Column(db.String(30), default='pending')
    # pending | shipped | in_transit | out_for_delivery | delivered | failed
    raw_response   = db.Column(db.Text)          # JSON blob
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    order = db.relationship('Order', backref='shipments')
    tracking_logs = db.relationship('TrackingLog', backref='shipment',
                                    lazy=True, cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Shipment {self.tracking_number} via {self.provider}>'


class TrackingLog(db.Model):
    """Every tracking poll/update is logged here."""
    __tablename__ = 'tracking_logs'
    id           = db.Column(db.Integer, primary_key=True)
    shipment_id  = db.Column(db.Integer, db.ForeignKey('shipments.id'), nullable=False, index=True)
    provider     = db.Column(db.String(50))
    status       = db.Column(db.String(30))
    event        = db.Column(db.Text)
    location     = db.Column(db.String(200))
    timestamp    = db.Column(db.String(50))      # courier's timestamp string
    raw          = db.Column(db.Text)
    polled_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<TrackingLog {self.status} @ {self.polled_at}>'


class CODTransaction(db.Model):
    """COD lifecycle tracking."""
    __tablename__ = 'cod_transactions'
    id           = db.Column(db.Integer, primary_key=True)
    order_id     = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False, index=True)
    cod_amount   = db.Column(db.Float, nullable=False)
    cod_status   = db.Column(db.String(20), default='pending')
    # pending | collected | remitted | failed
    collected_at = db.Column(db.DateTime, nullable=True)
    remitted_at  = db.Column(db.DateTime, nullable=True)
    notes        = db.Column(db.Text)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    order = db.relationship('Order', backref=db.backref('cod_transaction', uselist=False))

    def __repr__(self):
        return f'<CODTransaction ₱{self.cod_amount} {self.cod_status}>'


class CourierPerformance(db.Model):
    """Daily courier performance snapshot for admin stats."""
    __tablename__ = 'courier_performance'
    id           = db.Column(db.Integer, primary_key=True)
    courier      = db.Column(db.String(100), nullable=False)
    date         = db.Column(db.String(10), nullable=False)   # YYYY-MM-DD
    total_ships  = db.Column(db.Integer, default=0)
    delivered    = db.Column(db.Integer, default=0)
    failed       = db.Column(db.Integer, default=0)
    avg_days     = db.Column(db.Float, default=0.0)
    avg_fee      = db.Column(db.Float, default=0.0)

    def __repr__(self):
        return f'<CourierPerf {self.courier} {self.date}>'
