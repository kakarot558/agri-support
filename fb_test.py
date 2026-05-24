"""
fb_test.py — Paste your User Token below, then run: python fb_test.py

This will:
  1. Exchange User Token -> Page Access Token via /me/accounts
  2. Test an actual post to your Facebook Page
  3. Auto-patch config.py with the correct token (handles all formats)
"""
import requests, sys, os, re

# ── PASTE YOUR CURRENT USER TOKEN HERE ───────────────────────────────────────
USER_TOKEN = "EAALHWunapggBQ18uvnGCHrhkZCBv0EQGYXVhB2hZCy9mUFtSyvt5VnSs3SBKYocZATLU25ISv10dGWtNKKbXZA7qxkZARqzZCBiVXkCyy2nKT6hNGWlJ4ALhOcqaKSnwDlxHQr945qglu8SGCtQG5ZAAOH0oYkaLU5V4t6m6HtVMSxEBt2eD12TVYCpfnamXE1l"
PAGE_ID    = "1022925337569355"
# ─────────────────────────────────────────────────────────────────────────────

API = "https://graph.facebook.com/v21.0"
SEP = "─" * 65

def section(t): print(f"\n{SEP}\n  {t}\n{SEP}")
def ok(m, d=""): print(f"  ✅  {m}"); d and print(f"       {d}")
def fail(m, d=""): print(f"  ❌  {m}"); d and print(f"       {d}")

print(f"\n{'='*65}\n  FACEBOOK PAGE TOKEN EXCHANGER + POST TESTER\n{'='*65}")

if USER_TOKEN == "PASTE_YOUR_TOKEN_HERE":
    fail("TOKEN NOT SET — open fb_test.py and paste your User Token at the top")
    sys.exit(1)

# TEST 1: Connectivity
section("TEST 1: Internet connectivity")
try:
    requests.get("https://graph.facebook.com/", timeout=8)
    ok("Can reach graph.facebook.com")
except Exception as e:
    fail("Cannot reach Facebook", str(e)); sys.exit(1)

# TEST 2: Get Page Access Token via /me/accounts (the ONLY reliable method)
section("TEST 2: Fetching Page Access Token via /me/accounts")
print("  Graph API Explorer always gives User Tokens.")
print("  /me/accounts is the only way to get the real Page Token.\n")

r = requests.get(f"{API}/me/accounts",
                 params={"access_token": USER_TOKEN,
                         "fields": "id,name,access_token"},
                 timeout=10).json()

if "error" in r:
    err = r["error"]
    code = err.get("code", 0)
    fail(f"Error {code}: {err.get('message')}")
    if code == 190:
        print("\n  Your User Token expired. Get a new one:")
        print("  1. developers.facebook.com/tools/explorer")
        print("  2. App: agrii-support3  |  User or Page: User Token")
        print("  3. Permissions: pages_show_list, pages_manage_posts, pages_read_engagement")
        print("  4. Click Generate Access Token, paste new token at top of this file")
    sys.exit(1)

pages = r.get("data", [])
if not pages:
    fail("No Pages found under this account")
    print("  - You must be an ADMIN of the Facebook Page (not just Editor)")
    print(f"  - Check your role: facebook.com/{PAGE_ID} -> Settings -> Page Roles")
    sys.exit(1)

page_token = None
page_name  = None
print("  Pages this account manages:")
for p in pages:
    marker = "  <-- TARGET" if str(p.get("id")) == str(PAGE_ID) else ""
    print(f"    {p.get('name')} (ID: {p.get('id')}){marker}")
    if str(p.get("id")) == str(PAGE_ID):
        page_token = p.get("access_token")
        page_name  = p.get("name")

if not page_token:
    fail(f"Page {PAGE_ID} not found in the list above")
    print(f"  Verify PAGE_ID = '{PAGE_ID}' is correct")
    sys.exit(1)

ok(f"Got Page Access Token for '{page_name}'")

# TEST 3: Confirm PAGE type
section("TEST 3: Confirming token is PAGE type")
dbg = requests.get(f"{API}/debug_token",
                   params={"input_token": page_token, "access_token": USER_TOKEN},
                   timeout=10).json()
ttype = dbg.get("data", {}).get("type", "unknown")
exp   = dbg.get("data", {}).get("expires_at", 1)
exp_str = "Never expires (permanent)" if exp == 0 else f"Unix: {exp}"
if ttype == "PAGE":
    ok("Token type = PAGE", exp_str)
else:
    print(f"  ⚠️  Token type = {ttype} (unexpected, will try posting anyway)")

# TEST 4: Real post
section("TEST 4: Sending test post to Facebook Page")
caption = (
    "🌾 SYSTEM CHECK — Agri-Fortress Auto-Post ✅\n\n"
    "Automated test from the Agri-Fortress supply system.\n"
    "If you see this on your Page, posting is WORKING! 🎉\n\n"
    "#AgriFortress #AgriSupport #Philippines"
)
r = requests.post(f"{API}/{PAGE_ID}/feed",
                  data={"message": caption, "access_token": page_token},
                  timeout=20).json()

if "id" in r:
    ok("POST SENT!", f"Post ID: {r['id']}")
    print(f"\n  ✅ Check your Facebook Page — the post is LIVE!")
else:
    err = r.get("error", {})
    code = err.get("code", 0)
    fail(f"Post failed — Error {code}: {err.get('message')}")
    fixes = {
        10:  "App is in DEVELOPMENT mode.\n"
             "     Fix: developers.facebook.com -> app 'agrii-support3' -> toggle to LIVE mode",
        368: "Page restricted from posting. Check Meta Business Suite -> Page Quality.",
        200: "Permission denied. Confirm your role on the Page is Admin.",
        190: "Token invalid. Get a fresh User Token and re-run.",
    }
    if code in fixes: print(f"\n  FIX: {fixes[code]}")
    sys.exit(1)

# STEP 5: Patch config.py — handles ALL formats including os.environ.get(...) or 'token'
section("STEP 5: Patching config.py")
config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
if not os.path.exists(config_path):
    print(f"  config.py not found. Manually set:")
    print(f"  FACEBOOK_ACCESS_TOKEN = '{page_token}'")
else:
    with open(config_path, "r") as f:
        content = f.read()

    # Pattern 1: simple assignment  FACEBOOK_ACCESS_TOKEN = 'old'
    p1 = re.sub(
        r"(FACEBOOK_ACCESS_TOKEN\s*=\s*)['\"].*?['\"]",
        f"\\1'{page_token}'",
        content
    )
    # Pattern 2: os.environ.get(...) or 'old'  — replaces just the fallback string
    p2 = re.sub(
        r"(FACEBOOK_ACCESS_TOKEN\s*=\s*os\.environ\.get\([^)]+\)\s*or\s*)\\\s*\n\s*['\"].*?['\"]",
        f"\\1'{page_token}'",
        p1,
        flags=re.MULTILINE
    )
    # Pattern 3: same but on one line
    p3 = re.sub(
        r"(FACEBOOK_ACCESS_TOKEN\s*=\s*os\.environ\.get\([^)]+\)\s*or\s*)['\"].*?['\"]",
        f"\\1'{page_token}'",
        p2
    )

    if p3 != content:
        with open(config_path, "w") as f:
            f.write(p3)
        ok("config.py updated successfully!")
        print("\n  ⚡ RESTART Flask now:")
        print("     1. Press Ctrl+C in your Flask terminal")
        print("     2. Run: python app.py")
        print("     3. Auto-posting will work immediately")
    else:
        fail("Could not auto-patch (unusual config format)")
        print(f"\n  Manually find this line in config.py and replace the token:")
        print(f"  FACEBOOK_ACCESS_TOKEN = '{page_token}'")
        print(f"\n  ← Replace the long string after 'or' with the token above")

print(f"\n{'='*65}")
print("  COMPLETE! Your Page Token (save this):")
print(f"  {page_token}")
print(f"{'='*65}\n")