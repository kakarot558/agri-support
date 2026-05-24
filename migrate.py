"""
AgriFortress v10 Migration — SQLAlchemy 2.x safe.
Run: python migrate.py
"""
import os
os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance'), exist_ok=True)

from app import app, db
from sqlalchemy import text

def col(conn, t, c):
    return any(r[1]==c for r in conn.execute(text(f"PRAGMA table_info({t})")).fetchall())

def tbl(conn, t):
    return bool(conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),{'t':t}).fetchall())

def migrate():
    with app.app_context():
        db.create_all()
        print("All tables created / verified")
        with db.engine.connect() as c:
            for col_name, ctype in [
                ("email","VARCHAR(200)"),("full_name","VARCHAR(200)"),
                ("phone","VARCHAR(50)"),("is_active","BOOLEAN DEFAULT 1"),
                ("address_line","TEXT"),("address_city","VARCHAR(100)"),
                ("address_province","VARCHAR(100)"),("address_postal","VARCHAR(20)"),
            ]:
                if not col(c,'users',col_name):
                    c.execute(text(f"ALTER TABLE users ADD COLUMN {col_name} {ctype}"))
                    print(f"  + users.{col_name}")
            if not col(c,'orders','user_id'):
                c.execute(text("ALTER TABLE orders ADD COLUMN user_id INTEGER REFERENCES users(id)"))
                print("  + orders.user_id")
            # v9 caption columns
            for col_name, ctype in [
                ("caption_mode", "VARCHAR(10) DEFAULT 'auto'"),
                ("custom_caption", "TEXT"),
            ]:
                if not col(c, 'products', col_name):
                    c.execute(text(f"ALTER TABLE products ADD COLUMN {col_name} {ctype}"))
                    print(f"  + products.{col_name}")
            c.execute(text("UPDATE users SET role='Admin' WHERE role IS NULL OR role=''"))
            c.execute(text("UPDATE users SET is_active=1 WHERE is_active IS NULL"))
            c.commit()
        print("\nv10 Migration complete!")
        print("\nNew tables: shipments, tracking_logs, cod_transactions, courier_performance")
        print("New routes: /api/shipping-rates  /api/create-shipment  /api/jt-track  /admin/cod")
        print("\nAPI Keys (all optional — app works without them):")
        print("  Shippo   (free): goshippo.com/signup   -> set SHIPPO_API_KEY=...")
        print("  EasyPost (free): easypost.com/signup   -> set EASYPOST_API_KEY=...")
        print("  TrackingMore:    trackingmore.com       -> set TRACKINGMORE_API_KEY=...")

if __name__ == '__main__':
    migrate()
