import base64
import hashlib
import json
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, Optional
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

app = FastAPI(title="OnLoan M-Pesa App & Admin")

DB_NAME = "onloan.db"
ADMIN_SECRET = "admin2026"

# =====================================================================
# SAFARICOM DARAJA SANDBOX CREDENTIALS (Paste your keys inside the quotes)
# Leave blank to run in instant local simulator mode automatically.
# =====================================================================
DARAJA_CONSUMER_KEY = os.getenv("DARAJA_CONSUMER_KEY", "")
DARAJA_CONSUMER_SECRET = os.getenv("DARAJA_CONSUMER_SECRET", "")

# Official Safaricom Sandbox Defaults (Pre-configured)
DARAJA_STK_SHORTCODE = "174379"
DARAJA_STK_PASSKEY = "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"
DARAJA_CALLBACK_BASE = os.getenv("DARAJA_CALLBACK_BASE", "https://mydomain.com/onloan")

# Optional B2C Sandbox credentials
DARAJA_B2C_SHORTCODE = os.getenv("DARAJA_B2C_SHORTCODE", "600986")
DARAJA_B2C_INITIATOR = os.getenv("DARAJA_B2C_INITIATOR", "testapi")
DARAJA_B2C_SECURITY_CREDENTIAL = os.getenv("DARAJA_B2C_SECURITY_CREDENTIAL", "")

TIERS: Dict[int, Dict[str, Any]] = {
    1: {"limit": 1000, "fee": 100, "label": "Starter Tier 1"},
    2: {"limit": 2500, "fee": 80,  "label": "Silver Tier 2 (Discounted Fee)"},
    3: {"limit": 5000, "fee": 60,  "label": "Gold Tier 3 (Lowest Fee)"},
}


def get_daraja_token() -> Optional[str]:
    """Fetches a live OAuth Bearer token from Safaricom Daraja Sandbox."""
    if not DARAJA_CONSUMER_KEY or not DARAJA_CONSUMER_SECRET:
        return None
    url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    creds = f"{DARAJA_CONSUMER_KEY}:{DARAJA_CONSUMER_SECRET}".encode("utf-8")
    b64_creds = base64.b64encode(creds).decode("utf-8")
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {b64_creds}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return str(data.get("access_token", ""))
    except Exception as exc:
        print(f"[Daraja OAuth Warning] Falling back to simulator: {exc}")
        return None


def trigger_daraja_stk_push(msisdn: str, amount: int, loan_id: int) -> Dict[str, Any]:
    """Sends a real Lipa Na M-Pesa Online STK Push prompt if Daraja keys are configured."""
    token = get_daraja_token()
    if not token:
        return {"live": False, "receipt": f"STK{amount}OK"}

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    raw_pass = f"{DARAJA_STK_SHORTCODE}{DARAJA_STK_PASSKEY}{timestamp}".encode("utf-8")
    password = base64.b64encode(raw_pass).decode("utf-8")

    phone = msisdn.lstrip("+")
    if phone.startswith("0"):
        phone = "254" + phone[1:]

    payload = {
        "BusinessShortCode": DARAJA_STK_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": max(1, int(amount)),
        "PartyA": phone,
        "PartyB": DARAJA_STK_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": f"{DARAJA_CALLBACK_BASE}/webhooks/daraja/stk",
        "AccountReference": f"ONLOAN-{loan_id}",
        "TransactionDesc": f"OnLoan Repayment #{loan_id}",
    }

    req = urllib.request.Request(
        "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            checkout_id = str(res_data.get("CheckoutRequestID", f"STK{amount}LIVE"))
            return {"live": True, "receipt": checkout_id[:16], "raw": res_data}
    except urllib.error.HTTPError as err:
        err_body = err.read().decode("utf-8", errors="ignore")
        raise HTTPException(status_code=502, detail=f"Daraja STK Error: {err_body}")


def trigger_daraja_b2c(msisdn: str, amount: int, tier: int, loan_id: int) -> Dict[str, Any]:
    """Calls Daraja Sandbox B2C v3 API if keys + SecurityCredential are set; otherwise simulates."""
    token = get_daraja_token()
    if not token or not DARAJA_B2C_SECURITY_CREDENTIAL:
        return {"live": False, "receipt": f"B2C{amount}T{tier}"}

    phone = msisdn.lstrip("+")
    if phone.startswith("0"):
        phone = "254" + phone[1:]

    payload = {
        "OriginatorConversationID": f"ONLOAN-B2C-{loan_id}-{int(datetime.now().timestamp())}",
        "InitiatorName": DARAJA_B2C_INITIATOR,
        "SecurityCredential": DARAJA_B2C_SECURITY_CREDENTIAL,
        "CommandID": "BusinessPayment",
        "Amount": max(10, int(amount)),
        "PartyA": DARAJA_B2C_SHORTCODE,
        "PartyB": phone,
        "Remarks": f"OnLoan Disbursement #{loan_id}",
        "QueueTimeOutURL": f"{DARAJA_CALLBACK_BASE}/webhooks/daraja/b2c-timeout",
        "ResultURL": f"{DARAJA_CALLBACK_BASE}/webhooks/daraja/b2c-result",
        "Occasion": "MicroLoan",
    }

    req = urllib.request.Request(
        "https://sandbox.safaricom.co.ke/mpesa/b2c/v3/paymentrequest",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            conv_id = str(res_data.get("ConversationID", f"B2C{amount}LIVE"))
            return {"live": True, "receipt": conv_id[:16], "raw": res_data}
    except urllib.error.HTTPError as err:
        err_body = err.read().decode("utf-8", errors="ignore")
        raise HTTPException(status_code=502, detail=f"Daraja B2C Error: {err_body}")


def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.strip().encode("utf-8")).hexdigest()


def verify_admin(token: str) -> None:
    if token != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid Admin Password.")


def get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_NAME, timeout=10)


def init_db() -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS system_state (key TEXT PRIMARY KEY, val INTEGER)")
        cur.execute("INSERT OR IGNORE INTO system_state (key, val) VALUES ('mpesa_float', 50000)")
        cur.execute("INSERT OR IGNORE INTO system_state (key, val) VALUES ('fees_collected', 0)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS borrowers (
                msisdn TEXT PRIMARY KEY,
                tier INTEGER DEFAULT 1,
                national_id TEXT DEFAULT '12345678',
                pin_hash TEXT DEFAULT ''
            )
        """)
        cur.execute("PRAGMA table_info(borrowers)")
        b_cols = [str(col[1]) for col in cur.fetchall()]
        if "national_id" not in b_cols:
            cur.execute("ALTER TABLE borrowers ADD COLUMN national_id TEXT DEFAULT '12345678'")
        if "pin_hash" not in b_cols:
            cur.execute("ALTER TABLE borrowers ADD COLUMN pin_hash TEXT DEFAULT ''")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS active_loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msisdn TEXT,
                principal_due INTEGER,
                fee_due INTEGER,
                penalty_due INTEGER DEFAULT 0,
                days_elapsed INTEGER DEFAULT 0,
                status TEXT,
                disburse_receipt TEXT,
                last_repay_receipt TEXT
            )
        """)
        cur.execute("PRAGMA table_info(active_loans)")
        existing_cols = [str(col[1]) for col in cur.fetchall()]
        if "penalty_due" not in existing_cols:
            cur.execute("ALTER TABLE active_loans ADD COLUMN penalty_due INTEGER DEFAULT 0")
        if "days_elapsed" not in existing_cols:
            cur.execute("ALTER TABLE active_loans ADD COLUMN days_elapsed INTEGER DEFAULT 0")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS suspense_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payer_msisdn TEXT,
                account_ref TEXT,
                amount INTEGER,
                receipt TEXT,
                status TEXT DEFAULT 'UNALLOCATED',
                assigned_loan_id INTEGER,
                created_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS tx_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msisdn TEXT,
                tx_type TEXT,
                amount INTEGER,
                receipt TEXT,
                notes TEXT,
                created_at TEXT
            )
        """)
        conn.commit()


init_db()


class BorrowerAuthRequest(BaseModel):
    msisdn: str
    pin: str


class AdminAuthRequest(BaseModel):
    password: str


class LoanRequest(BaseModel):
    msisdn: str
    pin: str
    national_id: str
    principal: int


class RepayRequest(BaseModel):
    loan_id: int
    amount: int


class PaybillRequest(BaseModel):
    payer_msisdn: str
    account_ref: str
    amount: int


class ResolveSuspenseRequest(BaseModel):
    suspense_id: int
    loan_id: int


class FloatTopupRequest(BaseModel):
    amount: int


def process_loan_repayment(
    cur: sqlite3.Cursor,
    loan_id: int,
    amount: int,
    tx_label: str,
    receipt: str,
) -> Dict[str, Any]:
    cur.execute(
        "SELECT msisdn, principal_due, fee_due, COALESCE(penalty_due, 0), COALESCE(days_elapsed, 0), status "
        "FROM active_loans WHERE id = ?",
        (loan_id,),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Loan not found")

    msisdn = str(row[0])
    principal_due = int(row[1])
    fee_due = int(row[2])
    penalty_due = int(row[3])
    days_elapsed = int(row[4])
    prev_status = str(row[5])

    if prev_status == "PAID_OFF":
        raise HTTPException(status_code=400, detail="Loan is already paid off!")

    rem = int(amount)
    paid_p = min(rem, principal_due)
    rem -= paid_p

    paid_f = min(rem, fee_due)
    rem -= paid_f

    paid_pen = min(rem, penalty_due)

    new_p = principal_due - paid_p
    new_f = fee_due - paid_f
    new_pen = penalty_due - paid_pen
    new_total = new_p + new_f + new_pen
    new_status = "PAID_OFF" if new_total == 0 else prev_status
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute("UPDATE system_state SET val = val + ? WHERE key = 'fees_collected'", (paid_f + paid_pen,))
    cur.execute(
        "UPDATE active_loans SET principal_due = ?, fee_due = ?, penalty_due = ?, status = ?, last_repay_receipt = ? WHERE id = ?",
        (new_p, new_f, new_pen, new_status, receipt, loan_id),
    )

    note = f"Loan #{loan_id} • Allocated: KES {paid_p} Prin, KES {paid_f} Fee, KES {paid_pen} Pen"
    if new_status == "PAID_OFF":
        if prev_status == "ACTIVE":
            cur.execute("SELECT tier FROM borrowers WHERE msisdn = ?", (msisdn,))
            r_t = cur.fetchone()
            current_tier = int(r_t[0]) if r_t else 1
            next_tier = min(3, current_tier + 1)
            cur.execute("UPDATE borrowers SET tier = ? WHERE msisdn = ?", (next_tier, msisdn))
            note += f" • On-Time! Upgraded to Tier {next_tier}"
        else:
            note += " • Paid Late (Tier unchanged)"

    cur.execute(
        "INSERT INTO tx_history (msisdn, tx_type, amount, receipt, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (msisdn, tx_label, amount, receipt, note, now),
    )

    return {
        "loan_id": loan_id,
        "msisdn": msisdn,
        "status": new_status,
        "principal_due": new_p,
        "fee_due": new_f,
        "penalty_due": new_pen,
        "days_elapsed": days_elapsed,
        "total_due": new_total,
    }


@app.get("/manifest.json", response_class=JSONResponse)
def pwa_manifest() -> Dict[str, Any]:
    return {
        "name": "OnLoan Instant M-Pesa Credit",
        "short_name": "OnLoan",
        "description": "Instant Safaricom M-Pesa Micro-Loans with CBK Waterfall Allocation",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0f172a",
        "theme_color": "#1a73e8",
        "icons": [
            {
                "src": "/icon.svg",
                "sizes": "192x192 512x512",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    }


@app.get("/icon.svg")
def pwa_icon() -> Response:
    svg_data = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
      <rect width="512" height="512" rx="112" fill="#1e293b"/>
      <circle cx="256" cy="256" r="170" fill="#16a34a"/>
      <text x="256" y="295" font-family="Arial, sans-serif" font-size="120" font-weight="bold" fill="#ffffff" text-anchor="middle">KES</text>
    </svg>"""
    return Response(content=svg_data, media_type="image/svg+xml")


@app.get("/sw.js")
def service_worker() -> Response:
    js_code = """
    self.addEventListener('install', (e) => self.skipWaiting());
    self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
    self.addEventListener('fetch', (e) => {});
    """
    return Response(content=js_code, media_type="application/javascript")


@app.get("/", response_class=HTMLResponse)
def home_screen() -> str:
    daraja_mode = "LIVE DARAJA SANDBOX" if (DARAJA_CONSUMER_KEY and DARAJA_CONSUMER_SECRET) else "LOCAL SIMULATOR MODE"
    daraja_color = "#16a34a" if (DARAJA_CONSUMER_KEY and DARAJA_CONSUMER_SECRET) else "#475569"
    return f"""
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover" />
        <meta name="theme-color" content="#1a73e8" />
        <meta name="mobile-web-app-capable" content="yes" />
        <meta name="apple-mobile-web-app-capable" content="yes" />
        <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
        <link rel="manifest" href="/manifest.json" />
        <link rel="icon" type="image/svg+xml" href="/icon.svg" />
        <title>OnLoan App & Admin</title>
        <style>
          * {{ -webkit-tap-highlight-color: transparent; }}
          body {{ font-family: Arial, sans-serif; background: #f1f5f9; display: flex; flex-direction: column; align-items: center; padding: 14px; margin: 0; user-select: none; }}
          .nav-tabs {{ display: flex; gap: 8px; margin-bottom: 14px; background: #e2e8f0; padding: 5px; border-radius: 12px; width: 100%; max-width: 390px; box-sizing: border-box; }}
          .tab-btn {{ flex: 1; padding: 10px 12px; border: none; border-radius: 8px; cursor: pointer; font-weight: bold; font-size: 13px; background: transparent; color: #475569; margin: 0; }}
          .tab-btn.active {{ background: #1e293b; color: white; }}
          .phone {{ width: 100%; max-width: 390px; background: white; padding: 20px; border-radius: 18px; box-shadow: 0 8px 24px rgba(0,0,0,0.08); box-sizing: border-box; }}
          .admin-panel {{ width: 920px; max-width: 96vw; background: white; padding: 20px; border-radius: 18px; box-shadow: 0 8px 24px rgba(0,0,0,0.08); display: none; box-sizing: border-box; overflow-x: auto; }}
          h2 {{ color: #1a73e8; margin: 0 0 10px 0; }}
          .mode-pill {{ display: inline-block; padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: bold; color: white; background: {daraja_color}; margin-bottom: 10px; }}
          .install-banner {{ display: none; background: #0f172a; color: white; padding: 10px 14px; border-radius: 10px; margin-bottom: 12px; font-size: 12px; align-items: center; justify-content: space-between; }}
          .auth-card {{ background: #f8fafc; border: 1px solid #cbd5e1; padding: 16px; border-radius: 12px; margin-bottom: 14px; }}
          .tier-banner {{ background: #1e293b; color: white; padding: 12px; border-radius: 10px; margin-bottom: 14px; font-size: 13px; }}
          .tier-badge {{ background: #f59e0b; color: #000; font-weight: bold; padding: 2px 8px; border-radius: 6px; font-size: 11px; float: right; }}
          .repay-box {{ background: #f0fdf4; border: 1px solid #bbf7d0; padding: 14px; border-radius: 10px; margin-top: 16px; display: none; }}
          .paybill-box {{ background: #fffbeb; border: 1px solid #fde68a; padding: 14px; border-radius: 10px; margin-top: 16px; }}
          label {{ font-size: 12px; font-weight: bold; color: #444; display: block; margin-top: 10px; }}
          input, select, button {{ width: 100%; padding: 11px; margin-top: 6px; border-radius: 8px; border: 1px solid #ccc; box-sizing: border-box; font-size: 14px; }}
          .btn-apply {{ background: #1a73e8; color: white; font-weight: bold; border: none; cursor: pointer; margin-top: 12px; }}
          .btn-repay {{ background: #16a34a; color: white; font-weight: bold; border: none; cursor: pointer; margin-top: 10px; }}
          .btn-paybill {{ background: #d97706; color: white; font-weight: bold; border: none; cursor: pointer; margin-top: 10px; }}
          .btn-print {{ background: #334155; color: white; font-weight: bold; border: none; cursor: pointer; margin-top: 8px; font-size: 12px; padding: 9px; }}
          .status-badge {{ display: inline-block; padding: 4px 8px; border-radius: 6px; font-weight: bold; font-size: 11px; background: #fef08a; color: #854d0e; }}
          .overdue-badge {{ background: #fee2e2; color: #991b1b; }}
          .paid-badge {{ background: #dcfce7; color: #166534; }}
          .history-box {{ margin-top: 20px; border-top: 1px solid #e5e7eb; padding-top: 12px; }}
          .tx-item {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 9px; margin-top: 8px; font-size: 12px; }}
          .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 20px; }}
          .kpi-card {{ background: #f8fafc; border: 1px solid #e2e8f0; padding: 12px; border-radius: 12px; }}
          .kpi-title {{ font-size: 11px; color: #64748b; font-weight: bold; }}
          .kpi-val {{ font-size: 18px; font-weight: bold; color: #0f172a; margin-top: 6px; }}
          table {{ width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }}
          th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #e2e8f0; }}
          th {{ background: #f1f5f9; color: #334155; }}
        </style>
      </head>
      <body>

        <div class="nav-tabs">
          <button id="tab-borrower" class="tab-btn active" onclick="switchView('borrower')">Borrower App</button>
          <button id="tab-admin" class="tab-btn" onclick="switchView('admin')">Admin Dashboard</button>
        </div>

        <div id="borrower-view" class="phone">
          <div id="pwa-install-bar" class="install-banner">
            <span>Install <b>OnLoan</b> Full-Screen App</span>
            <button onclick="triggerPwaInstall()" style="width:auto; margin:0; padding:6px 12px; background:#16a34a; color:white; border:none; font-size:12px; font-weight:bold; cursor:pointer;">Install</button>
          </div>

          <div style="display:flex; justify-content:space-between; align-items:center;">
            <div>
              <h2 style="margin-bottom:4px;">OnLoan Instant</h2>
              <span class="mode-pill">{daraja_mode}</span>
            </div>
            <button id="btn-logout-borrower" onclick="logoutBorrower()" style="display:none; width:auto; padding:6px 12px; background:#ef4444; color:white; border:none; font-size:11px; cursor:pointer; margin:0;">Lock / Switch</button>
          </div>

          <div id="borrower-login-gate" class="auth-card">
            <strong style="font-size:14px; color:#0f172a;">Borrower PIN Sign-In</strong>
            <div style="font-size:12px; color:#64748b; margin-top:4px;">
              First-time phone numbers automatically register their 4-digit PIN on sign-in.
            </div>
            <label>Safaricom Phone Number</label>
            <input id="login-phone" type="tel" value="254712345678" placeholder="2547XXXXXXXX" />
            <label>4-Digit Security PIN</label>
            <input id="login-pin" type="password" inputmode="numeric" maxlength="4" value="1234" placeholder="e.g. 1234" />
            <button class="btn-apply" onclick="loginBorrower()">Unlock My Loan Account</button>
          </div>

          <div id="borrower-unlocked-content" style="display:none;">
            <div class="tier-banner">
              <span id="tier-name" class="tier-badge">Tier 1</span>
              <div><strong>Max Limit:</strong> KES <span id="tier-limit">1000</span></div>
              <div style="margin-top:4px; color:#cbd5e1;">
                MSISDN: <span id="active-msisdn"></span> • Fee: KES <span id="tier-fee">100</span>
              </div>
            </div>

            <div id="apply-section">
              <label>National ID Number (Used for Paybill Matching)</label>
              <input id="nat-id" inputmode="numeric" value="12345678" />
              <label>Select Qualified Loan Amount (KES)</label>
              <select id="amount"></select>
              <button class="btn-apply" onclick="applyLoan()">Accept & Send to M-Pesa</button>
              <button class="btn-print" onclick="openBorrowerStatement()">Print / Save Full Borrower Statement (PDF)</button>
            </div>

            <div id="repay-section" class="repay-box">
              <div style="display:flex; justify-content:space-between; align-items:center;">
                <strong>Loan #<span id="loan-id-display"></span> (Day <span id="loan-days">0</span>)</strong>
                <span id="loan-status" class="status-badge">ACTIVE</span>
              </div>
              <div style="margin-top:8px; font-size:13px; line-height:1.6;" id="loan-details"></div>

              <div id="stk-controls">
                <label>Direct App Repayment (STK Push KES)</label>
                <input id="repay-amount" type="number" inputmode="numeric" />
                <button class="btn-repay" onclick="repayLoan()">Pay Now via M-Pesa STK Push</button>
              </div>
              <button class="btn-print" style="background:#0f172a; margin-top:10px;" onclick="openLoanReceipt(currentLoanId)">View Official Loan Receipt & Voucher</button>
            </div>

            <div class="paybill-box">
              <strong style="color:#92400e; font-size:13px;">C2B Paybill 400200 Simulator (USSD / SIM Toolkit)</strong>
              <div style="font-size:11px; color:#78350f; margin-top:3px;">
                Type the borrower's exact National ID to auto-match, or type a wrong ID (e.g. <b>999999</b>) to test the Admin Unmatched Suspense Queue!
              </div>
              <label>Account Number (National ID Reference)</label>
              <input id="pb-account" inputmode="numeric" value="12345678" />
              <label>Amount Paid (KES)</label>
              <input id="pb-amount" type="number" inputmode="numeric" value="500" />
              <button class="btn-paybill" onclick="simulatePaybill()">Simulate C2B Paybill Payment</button>
            </div>

            <div class="history-box">
              <strong style="font-size:13px; color:#334155;">M-Pesa Receipts & History Log</strong>
              <div id="history-list"></div>
            </div>
          </div>
        </div>

        <div id="admin-view" class="admin-panel">
          <div id="admin-login-gate" class="auth-card" style="max-width:380px; margin:20px auto;">
            <h2 style="margin-top:0;">Admin Treasury Lock</h2>
            <div style="font-size:12px; color:#64748b; margin-bottom:8px;">
              Default demo password: <b>admin2026</b>
            </div>
            <label>Admin Password</label>
            <input id="admin-pass-input" type="password" value="admin2026" placeholder="Enter admin password" />
            <button class="btn-apply" style="background:#0f172a;" onclick="unlockAdmin()">Unlock Admin Dashboard</button>
          </div>

          <div id="admin-unlocked-content" style="display:none;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; flex-wrap:wrap; gap:8px;">
              <h2 style="margin:0;">OnLoan Treasury & Operations</h2>
              <div style="display:flex; gap:8px; flex-wrap:wrap;">
                <button onclick="simulateOverdue()" style="width:auto; padding:8px 14px; background:#dc2626; color:white; font-weight:bold; border:none; cursor:pointer;">+15 Days (Overdue & CBK Cap Sim)</button>
                <button onclick="topupFloat()" style="width:auto; padding:8px 14px; background:#0f172a; color:white; border:none; cursor:pointer;">+ Top Up KES 10,000 Float</button>
                <button onclick="lockAdmin()" style="width:auto; padding:8px 14px; background:#64748b; color:white; border:none; cursor:pointer;">Lock Admin</button>
              </div>
            </div>

            <div class="kpi-grid">
              <div class="kpi-card">
                <div class="kpi-title">M-Pesa Float</div>
                <div class="kpi-val" style="color:#16a34a;">KES <span id="adm-float">0</span></div>
              </div>
              <div class="kpi-card">
                <div class="kpi-title">Principal Out</div>
                <div class="kpi-val" style="color:#ea580c;">KES <span id="adm-principal-out">0</span></div>
              </div>
              <div class="kpi-card">
                <div class="kpi-title">Fees + Penalties Out</div>
                <div class="kpi-val" style="color:#dc2626;">KES <span id="adm-fees-out">0</span></div>
              </div>
              <div class="kpi-card">
                <div class="kpi-title">Unmatched Suspense</div>
                <div class="kpi-val" style="color:#d97706;">KES <span id="adm-suspense">0</span></div>
              </div>
              <div class="kpi-card">
                <div class="kpi-title">Revenue Collected</div>
                <div class="kpi-val" style="color:#2563eb;">KES <span id="adm-fees-earned">0</span></div>
              </div>
            </div>

            <h3 style="font-size:15px; margin-bottom:6px; color:#b45309;">Unmatched Paybill Suspense Queue (Requires Ops Assignment)</h3>
            <table>
              <thead>
                <tr><th>ID</th><th>Payer MSISDN</th><th>Typed Account Ref</th><th>Amount</th><th>Receipt</th><th>Assign to Open Loan</th></tr>
              </thead>
              <tbody id="adm-suspense-table"></tbody>
            </table>

            <h3 style="font-size:15px; margin:20px 0 6px 0; color:#1e293b;">Borrowers & Credit Tiers</h3>
            <table>
              <thead>
                <tr><th>MSISDN</th><th>National ID</th><th>PIN Status</th><th>Current Tier</th><th>Max Limit</th><th>Statement</th></tr>
              </thead>
              <tbody id="adm-borrowers-table"></tbody>
            </table>

            <h3 style="font-size:15px; margin:20px 0 6px 0; color:#1e293b;">All Loan Accounts</h3>
            <table>
              <thead>
                <tr><th>Loan</th><th>MSISDN</th><th>Age</th><th>Principal</th><th>Fee</th><th>Late Penalty</th><th>Total Due</th><th>Status</th><th>Receipt</th></tr>
              </thead>
              <tbody id="adm-loans-table"></tbody>
            </table>
          </div>
        </div>

        <script>
          let currentMsisdn = null;
          let currentPin = null;
          let currentLoanId = null;
          let adminToken = "";
          let deferredPrompt = null;

          if ('serviceWorker' in navigator) {{
            navigator.serviceWorker.register('/sw.js').catch(() => {{}});
          }}

          window.addEventListener('beforeinstallprompt', (e) => {{
            e.preventDefault();
            deferredPrompt = e;
            document.getElementById('pwa-install-bar').style.display = 'flex';
          }});

          async function triggerPwaInstall() {{
            if (!deferredPrompt) return;
            deferredPrompt.prompt();
            await deferredPrompt.userChoice;
            deferredPrompt = null;
            document.getElementById('pwa-install-bar').style.display = 'none';
          }}

          function switchView(mode) {{
            if (mode === 'admin') {{
              document.getElementById('borrower-view').style.display = 'none';
              document.getElementById('admin-view').style.display = 'block';
              document.getElementById('tab-borrower').className = 'tab-btn';
              document.getElementById('tab-admin').className = 'tab-btn active';
              if (adminToken) {{
                loadAdminDashboard();
              }}
            }} else {{
              document.getElementById('admin-view').style.display = 'none';
              document.getElementById('borrower-view').style.display = 'block';
              document.getElementById('tab-admin').className = 'tab-btn';
              document.getElementById('tab-borrower').className = 'tab-btn active';
              if (currentMsisdn && currentPin) {{
                loadProfile();
              }}
            }}
          }}

          async function loginBorrower() {{
            const msisdn = document.getElementById('login-phone').value.trim();
            const pin = document.getElementById('login-pin').value.trim();
            if (!msisdn || pin.length !== 4 || isNaN(Number(pin))) {{
              alert("Please enter a valid phone number and an exact 4-digit numeric PIN.");
              return;
            }}
            const res = await fetch('/api/auth/borrower', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{msisdn, pin}})
            }});
            if (!res.ok) {{
              const err = await res.json();
              alert(err.detail || "Invalid PIN!");
              return;
            }}
            const data = await res.json();
            currentMsisdn = msisdn;
            currentPin = pin;
            document.getElementById('borrower-login-gate').style.display = 'none';
            document.getElementById('borrower-unlocked-content').style.display = 'block';
            document.getElementById('btn-logout-borrower').style.display = 'block';
            if (data.status === 'REGISTERED_NEW_PIN') {{
              alert("Welcome! Your 4-digit PIN has been set for " + msisdn);
            }}
            await loadProfile();
          }}

          function logoutBorrower() {{
            currentMsisdn = null;
            currentPin = null;
            currentLoanId = null;
            document.getElementById('login-pin').value = '';
            document.getElementById('borrower-unlocked-content').style.display = 'none';
            document.getElementById('btn-logout-borrower').style.display = 'none';
            document.getElementById('borrower-login-gate').style.display = 'block';
          }}

          async function unlockAdmin() {{
            const password = document.getElementById('admin-pass-input').value.trim();
            const res = await fetch('/api/auth/admin', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{password}})
            }});
            if (!res.ok) {{
              const err = await res.json();
              alert(err.detail || "Incorrect Admin Password!");
              return;
            }}
            adminToken = password;
            document.getElementById('admin-login-gate').style.display = 'none';
            document.getElementById('admin-unlocked-content').style.display = 'block';
            await loadAdminDashboard();
          }}

          function lockAdmin() {{
            adminToken = "";
            document.getElementById('admin-pass-input').value = '';
            document.getElementById('admin-unlocked-content').style.display = 'none';
            document.getElementById('admin-login-gate').style.display = 'block';
          }}

          function openBorrowerStatement(customMsisdn) {{
            const msisdn = customMsisdn || currentMsisdn || "254712345678";
            window.open(`/statement?msisdn=${{encodeURIComponent(msisdn)}}`, '_blank');
          }}

          function openLoanReceipt(loanId) {{
            if (!loanId) return;
            window.open(`/receipt/${{loanId}}`, '_blank');
          }}

          async function loadAdminDashboard() {{
            const res = await fetch('/api/admin/summary', {{
              headers: {{'x-admin-token': adminToken}}
            }});
            if (!res.ok) {{
              lockAdmin();
              return;
            }}
            const data = await res.json();

            document.getElementById('adm-float').innerText = data.mpesa_float.toLocaleString();
            document.getElementById('adm-principal-out').innerText = data.principal_outstanding.toLocaleString();
            document.getElementById('adm-fees-out').innerText = data.fees_outstanding.toLocaleString();
            document.getElementById('adm-suspense').innerText = data.suspense_total.toLocaleString();
            document.getElementById('adm-fees-earned').innerText = data.fees_collected.toLocaleString();

            const openLoans = data.loans.filter(l => l.status !== 'PAID_OFF');

            if (data.suspense.length === 0) {{
              document.getElementById('adm-suspense-table').innerHTML =
                '<tr><td colspan="6" style="color:#64748b;">No unmatched Paybill payments in suspense.</td></tr>';
            }} else {{
              document.getElementById('adm-suspense-table').innerHTML = data.suspense.map(s => {{
                const optionsHtml = openLoans.map(l =>
                  `<option value="${{l.id}}">Loan #${{l.id}} (${{l.msisdn}} • Bal KES ${{l.total_due}})</option>`
                ).join('');
                const assignControl = openLoans.length > 0
                  ? `<div style="display:flex; gap:6px;">
                       <select id="assign-sel-${{s.id}}" style="margin:0; padding:6px; font-size:12px;">${{optionsHtml}}</select>
                       <button onclick="resolveSuspense(${{s.id}})" style="margin:0; padding:6px 10px; width:auto; background:#16a34a; color:white; border:none; font-size:12px; cursor:pointer;">Allocate</button>
                     </div>`
                  : `<span style="color:#94a3b8; font-size:12px;">No open loans available</span>`;

                return `
                  <tr>
                    <td>#${{s.id}}</td>
                    <td>${{s.payer_msisdn}}</td>
                    <td><b style="color:#dc2626;">${{s.account_ref}}</b></td>
                    <td><b>KES ${{s.amount}}</b></td>
                    <td>${{s.receipt}}</td>
                    <td>${{assignControl}}</td>
                  </tr>
                `;
              }}).join('');
            }}

            document.getElementById('adm-borrowers-table').innerHTML = data.borrowers.map(b => `
              <tr>
                <td><b>${{b.msisdn}}</b></td>
                <td>${{b.national_id}}</td>
                <td>${{b.pin_set ? '<span class="status-badge paid-badge">PIN Protected</span>' : '<span class="status-badge">Unset</span>'}}</td>
                <td>${{b.tier_label}}</td>
                <td>KES ${{b.limit}}</td>
                <td><button onclick="openBorrowerStatement('${{b.msisdn}}')" style="margin:0; padding:5px 10px; width:auto; background:#334155; color:white; border:none; font-size:11px; cursor:pointer;">Statement PDF</button></td>
              </tr>
            `).join('');

            document.getElementById('adm-loans-table').innerHTML = data.loans.map(l => {{
              let badgeClass = 'status-badge';
              if (l.status === 'PAID_OFF') badgeClass += ' paid-badge';
              if (l.status === 'OVERDUE') badgeClass += ' overdue-badge';
              return `
                <tr>
                  <td>#${{l.id}}</td>
                  <td>${{l.msisdn}}</td>
                  <td>Day ${{l.days_elapsed}}</td>
                  <td>KES ${{l.principal_due}}</td>
                  <td>KES ${{l.fee_due}}</td>
                  <td style="color:#dc2626; font-weight:bold;">KES ${{l.penalty_due}} ${{l.capped ? '(CAPPED)' : ''}}</td>
                  <td><b>KES ${{l.total_due}}</b></td>
                  <td><span class="${{badgeClass}}">${{l.status}}</span></td>
                  <td><button onclick="openLoanReceipt(${{l.id}})" style="margin:0; padding:5px 10px; width:auto; background:#0f172a; color:white; border:none; font-size:11px; cursor:pointer;">Print Receipt</button></td>
                </tr>
              `;
            }}).join('');
          }}

          async function resolveSuspense(suspenseId) {{
            const sel = document.getElementById(`assign-sel-${{suspenseId}}`);
            if (!sel || !sel.value) return;
            const loanId = parseInt(sel.value, 10);
            const res = await fetch('/api/admin/resolve-suspense', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json', 'x-admin-token': adminToken}},
              body: JSON.stringify({{suspense_id: suspenseId, loan_id: loanId}})
            }});
            if (!res.ok) {{
              const err = await res.json();
              alert(err.detail || "Failed to allocate suspense payment.");
              return;
            }}
            alert("Suspense payment allocated to Loan #" + loanId + "!");
            await loadAdminDashboard();
          }}

          async function simulateOverdue() {{
            const res = await fetch('/api/admin/simulate-overdue', {{
              method: 'POST',
              headers: {{'x-admin-token': adminToken}}
            }});
            const data = await res.json();
            alert(data.message);
            await loadAdminDashboard();
          }}

          async function topupFloat() {{
            await fetch('/api/admin/topup', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json', 'x-admin-token': adminToken}},
              body: JSON.stringify({{amount: 10000}})
            }});
            await loadAdminDashboard();
          }}

          async function loadProfile() {{
            if (!currentMsisdn || !currentPin) return;
            const res = await fetch(`/api/profile?msisdn=${{encodeURIComponent(currentMsisdn)}}&pin=${{encodeURIComponent(currentPin)}}`);
            if (!res.ok) {{
              logoutBorrower();
              return;
            }}
            const data = await res.json();

            document.getElementById('active-msisdn').innerText = data.msisdn;
            document.getElementById('tier-name').innerText = data.tier_label;
            document.getElementById('tier-limit').innerText = data.max_limit;
            document.getElementById('tier-fee').innerText = data.fee;
            document.getElementById('nat-id').value = data.national_id;
            document.getElementById('pb-account').value = data.national_id;

            const select = document.getElementById('amount');
            select.innerHTML = '';
            const options = [500, 1000, 2500, 5000].filter(amt => amt <= data.max_limit);
            options.forEach(amt => {{
              const opt = document.createElement('option');
              opt.value = amt;
              opt.text = `Receive KES ${{amt}} (Repay KES ${{amt + data.fee}})`;
              opt.selected = (amt === data.max_limit);
              select.appendChild(opt);
            }});

            if (data.active_loan) {{
              currentLoanId = data.active_loan.loan_id;
              renderLoanState(data.active_loan);
            }} else {{
              currentLoanId = null;
              document.getElementById('repay-section').style.display = 'none';
            }}
            renderHistory(data.history);
          }}

          async function applyLoan() {{
            const national_id = document.getElementById('nat-id').value.trim() || "12345678";
            const principal = parseInt(document.getElementById('amount').value || "1000", 10);
            const res = await fetch('/api/apply', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{msisdn: currentMsisdn, pin: currentPin, national_id, principal}})
            }});
            if (!res.ok) {{
              const err = await res.json();
              alert(err.detail || "Could not apply for loan.");
              return;
            }}
            await loadProfile();
          }}

          async function repayLoan() {{
            const rawVal = document.getElementById('repay-amount').value;
            const amount = parseInt(rawVal, 10);
            if (!amount || amount <= 0 || !currentLoanId) {{
              alert("Please enter a valid repayment amount.");
              return;
            }}
            const res = await fetch('/api/repay', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{loan_id: currentLoanId, amount}})
            }});
            if (!res.ok) {{
              const err = await res.json();
              alert(err.detail || "Repayment error.");
              return;
            }}
            const data = await res.json();
            renderLoanState(data);
            await loadProfile();
          }}

          async function simulatePaybill() {{
            const account_ref = document.getElementById('pb-account').value.trim();
            const amount = parseInt(document.getElementById('pb-amount').value || "0", 10);
            if (!account_ref || amount <= 0) {{
              alert("Enter a valid Account Reference and Amount.");
              return;
            }}
            const res = await fetch('/api/paybill', {{
              method: 'POST',
              headers: {{'Content-Type': 'application/json'}},
              body: JSON.stringify({{payer_msisdn: currentMsisdn, account_ref, amount}})
            }});
            const data = await res.json();
            alert(data.message);
            await loadProfile();
          }}

          function renderLoanState(loan) {{
            document.getElementById('repay-section').style.display = 'block';
            document.getElementById('loan-id-display').innerText = loan.loan_id;
            document.getElementById('loan-days').innerText = loan.days_elapsed || 0;
            const statusEl = document.getElementById('loan-status');
            statusEl.innerText = loan.status;

            if (loan.status === 'PAID_OFF') {{
              statusEl.className = 'status-badge paid-badge';
              document.getElementById('stk-controls').style.display = 'none';
            }} else if (loan.status === 'OVERDUE') {{
              statusEl.className = 'status-badge overdue-badge';
              document.getElementById('stk-controls').style.display = 'block';
              document.getElementById('repay-amount').value = loan.total_due;
            }} else {{
              statusEl.className = 'status-badge';
              document.getElementById('stk-controls').style.display = 'block';
              document.getElementById('repay-amount').value = loan.total_due;
            }}

            document.getElementById('loan-details').innerHTML = `
              <b>Principal Due (1st):</b> KES ${{loan.principal_due}}<br>
              <b>Fee Due (2nd):</b> KES ${{loan.fee_due}}<br>
              <b>Late Penalty (3rd):</b> <span style="color:#dc2626; font-weight:bold;">KES ${{loan.penalty_due || 0}}</span><br>
              <b>Total Balance:</b> <b>KES ${{loan.total_due}}</b>
            `;
          }}

          function renderHistory(items) {{
            const box = document.getElementById('history-list');
            if (!items || items.length === 0) {{
              box.innerHTML = '<div style="font-size:12px; color:#94a3b8; margin-top:6px;">No transactions yet.</div>';
              return;
            }}
            box.innerHTML = items.map(tx => `
              <div class="tx-item">
                <b>${{tx.tx_type}}</b> • KES ${{tx.amount}} <span style="float:right; color:#64748b;">${{tx.receipt}}</span><br>
                <span style="color:#475569;">${{tx.notes}}</span>
              </div>
            `).join('');
          }}
        </script>
      </body>
    </html>
    """


@app.post("/api/auth/borrower")
def auth_borrower(req: BorrowerAuthRequest) -> Dict[str, Any]:
    clean_msisdn = req.msisdn.strip()
    clean_pin = req.pin.strip()
    if not clean_msisdn or len(clean_pin) != 4 or not clean_pin.isdigit():
        raise HTTPException(status_code=400, detail="Enter a valid phone number and 4-digit numeric PIN.")

    hashed = hash_pin(clean_pin)
    default_nat_id = clean_msisdn[-8:] if len(clean_msisdn) >= 8 else "12345678"

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(pin_hash, '') FROM borrowers WHERE msisdn = ?", (clean_msisdn,))
        row = cur.fetchone()

        if not row:
            cur.execute(
                "INSERT INTO borrowers (msisdn, tier, national_id, pin_hash) VALUES (?, 1, ?, ?)",
                (clean_msisdn, default_nat_id, hashed),
            )
            conn.commit()
            return {"status": "REGISTERED_NEW_PIN", "msisdn": clean_msisdn}

        saved_hash = str(row[0])
        if not saved_hash:
            cur.execute("UPDATE borrowers SET pin_hash = ? WHERE msisdn = ?", (hashed, clean_msisdn))
            conn.commit()
            return {"status": "REGISTERED_NEW_PIN", "msisdn": clean_msisdn}

        if saved_hash != hashed:
            raise HTTPException(status_code=401, detail="Incorrect 4-digit PIN for this phone number!")

        return {"status": "AUTHENTICATED", "msisdn": clean_msisdn}


@app.post("/api/auth/admin")
def auth_admin(req: AdminAuthRequest) -> Dict[str, Any]:
    if req.password.strip() != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid Admin Password! Try: admin2026")
    return {"status": "UNLOCKED"}


@app.get("/statement", response_class=HTMLResponse)
def borrower_statement(msisdn: str = Query(default="254712345678")) -> str:
    clean_msisdn = msisdn.strip() or "254712345678"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S EAT")

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tier, COALESCE(national_id, '12345678') FROM borrowers WHERE msisdn = ?", (clean_msisdn,))
        b_row = cur.fetchone()
        tier = int(b_row[0]) if b_row else 1
        nat_id = str(b_row[1]) if b_row else "12345678"
        tier_info = TIERS.get(tier, TIERS[1])
        tier_label = str(tier_info["label"])
        tier_limit = int(tier_info["limit"])

        cur.execute(
            "SELECT id, principal_due, fee_due, COALESCE(penalty_due, 0), COALESCE(days_elapsed, 0), status, disburse_receipt, COALESCE(last_repay_receipt, 'N/A') "
            "FROM active_loans WHERE msisdn = ? ORDER BY id DESC",
            (clean_msisdn,),
        )
        loans = cur.fetchall()

        cur.execute(
            "SELECT tx_type, amount, receipt, notes, created_at FROM tx_history WHERE msisdn = ? ORDER BY id DESC",
            (clean_msisdn,),
        )
        txs = cur.fetchall()

    loans_rows_html = "".join(
        f"""
        <tr>
          <td>#{r[0]}</td>
          <td>Day {r[4]}</td>
          <td>KES {int(r[1]):,}</td>
          <td>KES {int(r[2]):,}</td>
          <td>KES {int(r[3]):,}</td>
          <td><b>KES {(int(r[1]) + int(r[2]) + int(r[3])):,}</b></td>
          <td><b>{r[5]}</b></td>
          <td>{r[6]}</td>
          <td>{r[7]}</td>
        </tr>
        """
        for r in loans
    ) or '<tr><td colspan="9">No loans recorded for this MSISDN.</td></tr>'

    tx_rows_html = "".join(
        f"""
        <tr>
          <td>{t[4]}</td>
          <td><b>{t[0]}</b></td>
          <td>KES {int(t[1]):,}</td>
          <td><code>{t[2]}</code></td>
          <td>{t[3]}</td>
        </tr>
        """
        for t in txs
    ) or '<tr><td colspan="5">No M-Pesa transactions recorded yet.</td></tr>'

    return f"""
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>OnLoan Statement - {clean_msisdn}</title>
        <style>
          body {{ font-family: Arial, sans-serif; color: #0f172a; max-width: 820px; margin: 24px auto; padding: 24px; border: 1px solid #cbd5e1; border-radius: 12px; }}
          .header {{ display: flex; justify-content: space-between; align-items: flex-start; border-bottom: 2px solid #1e293b; padding-bottom: 14px; margin-bottom: 18px; }}
          .meta-box {{ background: #f8fafc; border: 1px solid #e2e8f0; padding: 12px 16px; border-radius: 8px; margin-bottom: 18px; font-size: 13px; line-height: 1.7; }}
          table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 12px; }}
          th, td {{ border: 1px solid #cbd5e1; padding: 8px; text-align: left; }}
          th {{ background: #f1f5f9; }}
          .print-bar {{ margin-bottom: 16px; display: flex; gap: 10px; }}
          .btn {{ background: #1a73e8; color: white; border: none; padding: 10px 18px; border-radius: 6px; font-weight: bold; cursor: pointer; }}
          @media print {{ .print-bar {{ display: none; }} body {{ border: none; margin: 0; padding: 0; }} }}
        </style>
      </head>
      <body>
        <div class="print-bar">
          <button class="btn" onclick="window.print()">Print / Save Statement as PDF</button>
          <button class="btn" style="background:#475569;" onclick="window.close()">Close Tab</button>
        </div>
        <div class="header">
          <div>
            <h2 style="margin:0; color:#1a73e8;">OnLoan Digital Credit — Official Borrower Statement</h2>
            <div style="font-size:12px; color:#475569; margin-top:4px;">CBK Non-Deposit Taking Credit Provider Compliant Allocation Ledger</div>
          </div>
          <div style="text-align:right; font-size:12px;">
            <b>Generated:</b> {now_str}<br>
            <b>Paybill Rail:</b> 400200
          </div>
        </div>

        <div class="meta-box">
          <b>Borrower MSISDN:</b> {clean_msisdn} &nbsp;|&nbsp;
          <b>National ID Ref:</b> {nat_id} &nbsp;|&nbsp;
          <b>Credit Tier:</b> {tier_label} (Max Limit KES {tier_limit:,})
        </div>

        <h3 style="font-size:14px; margin-bottom:4px;">1. Loan Account Summary</h3>
        <table>
          <thead>
            <tr>
              <th>Loan ID</th><th>Age</th><th>Principal Due</th><th>Fee Due</th><th>Late Penalty</th><th>Total Balance</th><th>Status</th><th>B2C Receipt</th><th>Last Repay Ref</th>
            </tr>
          </thead>
          <tbody>{loans_rows_html}</tbody>
        </table>

        <h3 style="font-size:14px; margin:20px 0 4px 0;">2. Chronological M-Pesa Transaction & Waterfall Allocation Ledger</h3>
        <table>
          <thead>
            <tr><th>Timestamp</th><th>Transaction Type</th><th>Amount (KES)</th><th>M-Pesa Receipt</th><th>CBK Allocation / Audit Notes</th></tr>
          </thead>
          <tbody>{tx_rows_html}</tbody>
        </table>
      </body>
    </html>
    """


@app.get("/receipt/{loan_id}", response_class=HTMLResponse)
def loan_receipt_voucher(loan_id: int) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S EAT")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT l.id, l.msisdn, l.principal_due, l.fee_due, COALESCE(l.penalty_due, 0),
                   COALESCE(l.days_elapsed, 0), l.status, l.disburse_receipt,
                   COALESCE(l.last_repay_receipt, 'NONE'), COALESCE(b.national_id, '12345678')
            FROM active_loans l
            LEFT JOIN borrowers b ON l.msisdn = b.msisdn
            WHERE l.id = ?
            """,
            (loan_id,),
        )
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Loan not found")

    total_bal = int(r[2]) + int(r[3]) + int(r[4])
    clearance_note = (
        "CLEARED IN FULL — ZERO BALANCE OUTSTANDING"
        if str(r[6]) == "PAID_OFF"
        else f"ACTIVE OBLIGATION — KES {total_bal:,} OUTSTANDING"
    )

    return f"""
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>OnLoan Receipt Voucher #{loan_id}</title>
        <style>
          body {{ font-family: Arial, sans-serif; color: #0f172a; max-width: 520px; margin: 30px auto; padding: 24px; border: 2px solid #1e293b; border-radius: 12px; }}
          .row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px dashed #cbd5e1; font-size: 13px; }}
          .stamp {{ margin-top: 16px; padding: 10px; text-align: center; font-weight: bold; border-radius: 8px; background: {'#dcfce7' if str(r[6]) == 'PAID_OFF' else '#fef08a'}; color: {'#166534' if str(r[6]) == 'PAID_OFF' else '#854d0e'}; }}
          .btn {{ background: #1a73e8; color: white; border: none; padding: 10px 16px; border-radius: 6px; font-weight: bold; cursor: pointer; margin-bottom: 14px; }}
          @media print {{ .no-print {{ display: none; }} body {{ border: 1px solid #000; margin: 0; }} }}
        </style>
      </head>
      <body>
        <div class="no-print" style="display:flex; gap:8px;">
          <button class="btn" onclick="window.print()">Print / Save Receipt Voucher (PDF)</button>
          <button class="btn" style="background:#475569;" onclick="window.close()">Close</button>
        </div>
        <h2 style="margin:0; color:#1a73e8;">OnLoan M-Pesa Loan Voucher</h2>
        <div style="font-size:12px; color:#64748b; margin-bottom:14px;">Issued: {now_str}</div>

        <div class="row"><span>Loan Account Number:</span><b>#{r[0]}</b></div>
        <div class="row"><span>Borrower MSISDN:</span><b>{r[1]}</b></div>
        <div class="row"><span>National ID Reference:</span><b>{r[9]}</b></div>
        <div class="row"><span>Loan Age:</span><b>Day {r[5]}</b></div>
        <div class="row"><span>B2C Disbursement Receipt:</span><b>{r[7]}</b></div>
        <div class="row"><span>Latest Repayment Receipt:</span><b>{r[8]}</b></div>
        <div class="row"><span>Principal Balance Remaining:</span><b>KES {int(r[2]):,}</b></div>
        <div class="row"><span>Origination Fee Remaining:</span><b>KES {int(r[3]):,}</b></div>
        <div class="row"><span>Late Penalty Remaining (CBK Capped):</span><b>KES {int(r[4]):,}</b></div>
        <div class="row" style="font-size:15px; border-bottom:2px solid #0f172a;"><span>Total Balance Payable:</span><b>KES {total_bal:,}</b></div>

        <div class="stamp">{clearance_note}</div>
      </body>
    </html>
    """


@app.post("/api/paybill")
def receive_paybill(req: PaybillRequest) -> Dict[str, Any]:
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than KES 0")

    clean_ref = req.account_ref.strip()
    clean_payer = req.payer_msisdn.strip()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    receipt = f"C2B{req.amount}PB"

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE system_state SET val = val + ? WHERE key = 'mpesa_float'", (req.amount,))

        cur.execute(
            """
            SELECT l.id
            FROM active_loans l
            JOIN borrowers b ON l.msisdn = b.msisdn
            WHERE b.national_id = ? AND l.status IN ('ACTIVE', 'OVERDUE')
            ORDER BY l.id DESC LIMIT 1
            """,
            (clean_ref,),
        )
        matched_loan = cur.fetchone()

        if matched_loan:
            loan_id = int(matched_loan[0])
            res = process_loan_repayment(cur, loan_id, req.amount, "REPAYMENT (PAYBILL)", receipt)
            conn.commit()
            return {
                "status": "MATCHED",
                "message": f"Paybill payment of KES {req.amount} auto-matched to National ID {clean_ref} (Loan #{loan_id})!",
                "loan": res,
            }

        cur.execute(
            """
            INSERT INTO suspense_payments (payer_msisdn, account_ref, amount, receipt, status, created_at)
            VALUES (?, ?, ?, ?, 'UNALLOCATED', ?)
            """,
            (clean_payer, clean_ref, req.amount, receipt, now),
        )
        cur.execute(
            """
            INSERT INTO tx_history (msisdn, tx_type, amount, receipt, notes, created_at)
            VALUES (?, 'PAYBILL SUSPENSE', ?, ?, ?, ?)
            """,
            (clean_payer, req.amount, receipt, f"Unmatched Account Ref '{clean_ref}' parked in Admin Suspense", now),
        )
        conn.commit()
        return {
            "status": "SUSPENSE",
            "message": f"Account Ref '{clean_ref}' did not match an active loan! KES {req.amount} parked in Admin Unmatched Suspense Queue.",
        }


@app.post("/api/admin/resolve-suspense")
def resolve_suspense(req: ResolveSuspenseRequest, x_admin_token: str = Header(default="")) -> Dict[str, Any]:
    verify_admin(x_admin_token)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT amount, receipt, status, account_ref FROM suspense_payments WHERE id = ?",
            (req.suspense_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Suspense item not found")

        amount = int(row[0])
        receipt = str(row[1])
        status = str(row[2])
        acc_ref = str(row[3])

        if status != "UNALLOCATED":
            raise HTTPException(status_code=400, detail="Suspense payment already allocated")

        res = process_loan_repayment(
            cur,
            req.loan_id,
            amount,
            f"SUSPENSE RECONCILED ({acc_ref})",
            receipt,
        )
        cur.execute(
            "UPDATE suspense_payments SET status = 'ALLOCATED', assigned_loan_id = ? WHERE id = ?",
            (req.loan_id, req.suspense_id),
        )
        conn.commit()
        return {"status": "RESOLVED", "loan": res}


@app.post("/api/admin/simulate-overdue")
def simulate_overdue(x_admin_token: str = Header(default="")) -> Dict[str, Any]:
    verify_admin(x_admin_token)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, msisdn, principal_due, fee_due, COALESCE(penalty_due, 0), COALESCE(days_elapsed, 0) "
            "FROM active_loans WHERE status IN ('ACTIVE', 'OVERDUE')"
        )
        rows = cur.fetchall()
        if not rows:
            return {"message": "No active loans to advance! Disburse a loan in the Borrower App first."}

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updated_count = 0

        for loan_id, msisdn, p_due, f_due, pen_due, days in rows:
            new_days = int(days) + 15
            raw_step_penalty = int(int(p_due) * 0.10)
            max_allowed_penalty = max(0, int(p_due) - int(f_due))
            new_penalty = min(int(pen_due) + raw_step_penalty, max_allowed_penalty)
            added_penalty = new_penalty - int(pen_due)

            cur.execute(
                "UPDATE active_loans SET days_elapsed = ?, penalty_due = ?, status = 'OVERDUE' WHERE id = ?",
                (new_days, new_penalty, loan_id),
            )
            note = (
                f"Loan #{loan_id} • Day {new_days}: +KES {added_penalty} late fee"
                if new_penalty < max_allowed_penalty
                else f"Loan #{loan_id} • Day {new_days}: CBK Cap Reached (Charges capped at KES {p_due})"
            )
            cur.execute(
                "INSERT INTO tx_history (msisdn, tx_type, amount, receipt, notes, created_at) VALUES (?, 'OVERDUE PENALTY', ?, ?, ?, ?)",
                (msisdn, added_penalty, f"LATE-D{new_days}", note, now),
            )
            updated_count += 1

        conn.commit()
        return {"message": f"Advanced {updated_count} loan(s) by +15 days and applied CBK-capped late penalties."}


@app.get("/api/admin/summary")
def admin_summary(x_admin_token: str = Header(default="")) -> Dict[str, Any]:
    verify_admin(x_admin_token)
    with get_conn() as conn:
        cur = conn.cursor()

        cur.execute("SELECT val FROM system_state WHERE key = 'mpesa_float'")
        r_float = cur.fetchone()
        mpesa_float = int(r_float[0]) if r_float else 50000

        cur.execute("SELECT val FROM system_state WHERE key = 'fees_collected'")
        r_fees = cur.fetchone()
        fees_collected = int(r_fees[0]) if r_fees else 0

        cur.execute(
            "SELECT COALESCE(SUM(principal_due), 0), COALESCE(SUM(fee_due + COALESCE(penalty_due, 0)), 0) "
            "FROM active_loans WHERE status IN ('ACTIVE', 'OVERDUE')"
        )
        r_totals = cur.fetchone()
        p_out = int(r_totals[0]) if r_totals else 0
        f_out = int(r_totals[1]) if r_totals else 0

        cur.execute("SELECT COALESCE(SUM(amount), 0) FROM suspense_payments WHERE status = 'UNALLOCATED'")
        r_susp = cur.fetchone()
        suspense_total = int(r_susp[0]) if r_susp else 0

        cur.execute(
            "SELECT id, payer_msisdn, account_ref, amount, receipt, created_at "
            "FROM suspense_payments WHERE status = 'UNALLOCATED' ORDER BY id DESC"
        )
        suspense = [
            {
                "id": int(r[0]),
                "payer_msisdn": str(r[1]),
                "account_ref": str(r[2]),
                "amount": int(r[3]),
                "receipt": str(r[4]),
                "created_at": str(r[5]),
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            "SELECT msisdn, tier, COALESCE(national_id, '12345678'), COALESCE(pin_hash, '') "
            "FROM borrowers ORDER BY tier DESC, msisdn ASC"
        )
        borrowers = [
            {
                "msisdn": str(r[0]),
                "tier": int(r[1]),
                "national_id": str(r[2]),
                "pin_set": bool(r[3]),
                "tier_label": str(TIERS.get(int(r[1]), TIERS[1])["label"]),
                "limit": int(TIERS.get(int(r[1]), TIERS[1])["limit"]),
                "fee": int(TIERS.get(int(r[1]), TIERS[1])["fee"]),
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            "SELECT id, msisdn, principal_due, fee_due, COALESCE(penalty_due, 0), COALESCE(days_elapsed, 0), status, disburse_receipt "
            "FROM active_loans ORDER BY id DESC"
        )
        loans = [
            {
                "id": int(r[0]),
                "msisdn": str(r[1]),
                "principal_due": int(r[2]),
                "fee_due": int(r[3]),
                "penalty_due": int(r[4]),
                "days_elapsed": int(r[5]),
                "total_due": int(r[2]) + int(r[3]) + int(r[4]),
                "status": str(r[6]),
                "receipt": str(r[7]),
                "capped": (int(r[3]) + int(r[4])) >= int(r[2]) and int(r[2]) > 0,
            }
            for r in cur.fetchall()
        ]

        return {
            "mpesa_float": mpesa_float,
            "principal_outstanding": p_out,
            "fees_outstanding": f_out,
            "suspense_total": suspense_total,
            "fees_collected": fees_collected,
            "suspense": suspense,
            "borrowers": borrowers,
            "loans": loans,
        }


@app.post("/api/admin/topup")
def topup_float(req: FloatTopupRequest, x_admin_token: str = Header(default="")) -> Dict[str, Any]:
    verify_admin(x_admin_token)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE system_state SET val = val + ? WHERE key = 'mpesa_float'", (req.amount,))
        conn.commit()
    return {"status": "ok"}


@app.get("/api/profile")
def get_profile(msisdn: str = Query(...), pin: str = Query(...)) -> Dict[str, Any]:
    clean_msisdn = msisdn.strip()
    hashed = hash_pin(pin)
    default_nat_id = clean_msisdn[-8:] if len(clean_msisdn) >= 8 else "12345678"

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT tier, COALESCE(national_id, ?), COALESCE(pin_hash, '') FROM borrowers WHERE msisdn = ?",
            (default_nat_id, clean_msisdn),
        )
        r_tier = cur.fetchone()
        if not r_tier or str(r_tier[2]) != hashed:
            raise HTTPException(status_code=401, detail="Invalid PIN session.")

        tier = int(r_tier[0])
        national_id = str(r_tier[1])
        tier_info = TIERS.get(tier, TIERS[1])

        cur.execute(
            "SELECT id, principal_due, fee_due, COALESCE(penalty_due, 0), COALESCE(days_elapsed, 0), status "
            "FROM active_loans WHERE msisdn = ? ORDER BY id DESC LIMIT 1",
            (clean_msisdn,),
        )
        loan_row = cur.fetchone()
        active_loan: Optional[Dict[str, Any]] = None
        if loan_row:
            p_due = int(loan_row[1])
            f_due = int(loan_row[2])
            pen_due = int(loan_row[3])
            active_loan = {
                "loan_id": int(loan_row[0]),
                "principal_due": p_due,
                "fee_due": f_due,
                "penalty_due": pen_due,
                "days_elapsed": int(loan_row[4]),
                "total_due": p_due + f_due + pen_due,
                "status": str(loan_row[5]),
            }

        cur.execute(
            "SELECT tx_type, amount, receipt, notes, created_at FROM tx_history WHERE msisdn = ? ORDER BY id DESC LIMIT 6",
            (clean_msisdn,),
        )
        history = [
            {"tx_type": r[0], "amount": r[1], "receipt": r[2], "notes": r[3], "time": r[4]}
            for r in cur.fetchall()
        ]

        return {
            "msisdn": clean_msisdn,
            "national_id": national_id,
            "tier": tier,
            "tier_label": str(tier_info["label"]),
            "max_limit": int(tier_info["limit"]),
            "fee": int(tier_info["fee"]),
            "active_loan": active_loan,
            "history": history,
        }


@app.post("/api/apply")
def apply_loan(req: LoanRequest) -> Dict[str, Any]:
    clean_msisdn = req.msisdn.strip()
    clean_nat_id = req.national_id.strip() or "12345678"
    hashed = hash_pin(req.pin)

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tier, COALESCE(pin_hash, '') FROM borrowers WHERE msisdn = ?", (clean_msisdn,))
        r_b = cur.fetchone()
        if not r_b or str(r_b[1]) != hashed:
            raise HTTPException(status_code=401, detail="Unauthorized: PIN verification failed.")

        tier = int(r_b[0])
        tier_info = TIERS.get(tier, TIERS[1])
        cur.execute("UPDATE borrowers SET national_id = ? WHERE msisdn = ?", (clean_nat_id, clean_msisdn))

        cur.execute(
            "SELECT id FROM active_loans WHERE msisdn = ? AND status IN ('ACTIVE', 'OVERDUE')",
            (clean_msisdn,),
        )
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Please repay your current open loan first.")

        cur.execute("SELECT val FROM system_state WHERE key = 'mpesa_float'")
        r_float = cur.fetchone()
        current_float = int(r_float[0]) if r_float else 0

        principal = min(req.principal, int(tier_info["limit"]))
        if current_float < principal:
            raise HTTPException(status_code=400, detail="Insufficient M-Pesa B2C Float! Top up in Admin Dashboard.")

        fee = int(tier_info["fee"])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cur.execute("UPDATE system_state SET val = val - ? WHERE key = 'mpesa_float'", (principal,))
        cur.execute(
            "INSERT INTO active_loans (msisdn, principal_due, fee_due, penalty_due, days_elapsed, status, disburse_receipt) "
            "VALUES (?, ?, ?, 0, 0, 'ACTIVE', 'PENDING')",
            (clean_msisdn, principal, fee),
        )
        loan_id = int(cur.lastrowid or 0)

        b2c_res = trigger_daraja_b2c(clean_msisdn, principal, tier, loan_id)
        receipt = str(b2c_res["receipt"])
        tx_label = "DISBURSEMENT (DARAJA B2C)" if b2c_res["live"] else "DISBURSEMENT (B2C)"

        cur.execute("UPDATE active_loans SET disburse_receipt = ? WHERE id = ?", (receipt, loan_id))
        cur.execute(
            "INSERT INTO tx_history (msisdn, tx_type, amount, receipt, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (clean_msisdn, tx_label, principal, receipt, f"Loan #{loan_id} sent to M-Pesa (Fee KES {fee})", now),
        )
        conn.commit()
        return {"loan_id": loan_id, "status": "ACTIVE", "daraja_live": b2c_res["live"]}


@app.post("/api/repay")
def repay_loan(req: RepayRequest) -> Dict[str, Any]:
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Enter an amount greater than KES 0")

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT msisdn FROM active_loans WHERE id = ?", (req.loan_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Loan not found")
        borrower_msisdn = str(row[0])

        stk_res = trigger_daraja_stk_push(borrower_msisdn, req.amount, req.loan_id)
        receipt = str(stk_res["receipt"])
        label = "REPAYMENT (DARAJA STK)" if stk_res["live"] else "REPAYMENT (STK)"

        cur.execute("UPDATE system_state SET val = val + ? WHERE key = 'mpesa_float'", (req.amount,))
        res = process_loan_repayment(cur, req.loan_id, req.amount, label, receipt)
        conn.commit()
        return res


@app.post("/webhooks/daraja/stk")
def daraja_stk_callback(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Receives async Daraja STK Push callback JSON from Safaricom."""
    stk_cb = payload.get("Body", {}).get("stkCallback", {})
    result_code = stk_cb.get("ResultCode", -1)
    if result_code == 0:
        items = stk_cb.get("CallbackMetadata", {}).get("Item", [])
        meta = {str(i.get("Name")): i.get("Value") for i in items if isinstance(i, dict)}
        print(f"[Daraja STK Confirmed] Receipt={meta.get('MpesaReceiptNumber')} Amount={meta.get('Amount')}")
    else:
        print(f"[Daraja STK Cancelled/Failed] Code={result_code} Desc={stk_cb.get('ResultDesc')}")
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.post("/webhooks/daraja/b2c-result")
def daraja_b2c_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Receives async Daraja B2C Result callback JSON from Safaricom."""
    res = payload.get("Result", {})
    print(f"[Daraja B2C Callback] Code={res.get('ResultCode')} Desc={res.get('ResultDesc')}")
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.post("/webhooks/daraja/b2c-timeout")
def daraja_b2c_timeout(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Receives async Daraja B2C QueueTimeout notification."""
    print(f"[Daraja B2C Timeout] Payload={payload}")
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


if __name__ == "__main__":
    import threading
    import webbrowser
    import uvicorn

    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:8000")).start()
    uvicorn.run(app, host="0.0.0.0", port=8000)