# AgriFortress v5 — Changelog

## What Changed from v4

### 🛒 Order System (MAJOR)
- **Removed**: "Order via Messenger" flow entirely
- **Added**: Full online order form at `/order/<product_id>`
  - Customer name, phone, email, address fields
  - Quantity selector with live total calculator
  - Shipping method picker (MSC · LBC · J&T · Store Pickup)
  - Payment options: COD, GCash, Bank Transfer
  - Special notes field
- **Added**: Order Confirmation page with order number
- **Added**: Order tracking page at `/order/track`

### 🚢 MSC Shipping Integration
- **Shipping options** on every order with flat Philippine rates:
  - MSC Shipping: ₱150 (3–5 days)
  - LBC Express: ₱120 (2–4 days)
  - J&T Express: ₱100 (2–3 days)
  - Store Pickup: FREE
- **Live MSC tracking** via `/api/msc-track` (free public API, no key required)
- **Admin tracking**: Check MSC status directly from order detail page

### 📦 Inventory Management
- **New page**: `/admin/inventory` — full stock overview
  - Stock level bars with color coding (green/amber/red)
  - Pending orders per product (reserved stock)
  - Total units sold per product
  - Inventory value calculator
  - Quick stock adjustment (add or set) per product
- **Auto stock deduction** on order placement
- **Auto stock restore** on order cancellation

### 🗃️ Admin Orders Panel
- **New pages**: `/admin/orders` and `/admin/orders/<id>`
  - Status tabs: Pending / Confirmed / Processing / Shipped / Delivered / Cancelled
  - Search by order #, customer name, or phone
  - Update order status + tracking number + payment status
  - Live MSC tracking lookup from order detail
- **Dashboard**: Orders/Shipped stats + Recent Orders table

### 🗄️ Database
- **New tables**: `orders`, `order_items`
- **Order model** includes: customer info, shipping, payment, status, tracking
- **OrderItem model** snapshots product name/price at time of order

### 🧩 Other Improvements
- "Track Order" added to public nav bar
- Homepage contact section now shows "Order Online" + "Track My Order" CTAs
- Product detail "Order via Messenger" button → "Order Now" (links to order form)

## How to Run Migrations
Run `python migrate.py` after deploying — the script calls `db.create_all()` which
will add the new `orders` and `order_items` tables without dropping existing data.

## Free API Used
- **MSC Tracking**: `https://api.mscshipping.com/tracking/v1/track` (public, no key)
- **AfterShip fallback**: `https://track.aftership.com/api/v1/trackings/<no>` (public sandbox)
