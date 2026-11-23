from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

APP_NAME = "OnLoan"
DB_PATH = os.getenv("ONLOAN_DB_PATH", "onloan.db")
OTP_PEPPER = os.getenv("OTP_PEPPER", "onloan-dev-otp-pepper-change-in-prod")
PIN_PEPPER = os.getenv("PIN_PEPPER", "onloan-dev-pin-pepper-change-in-prod")
ADMIN_PASSCODE = os.getenv("ONLOAN_ADMIN_PIN", "2026")
SANDBOX_MODE = os.getenv("SANDBOX_MODE", "true").lower() == "true"

DARAJA_CONSUMER_KEY = os.getenv(
    "DARAJA_CONSUMER_KEY", "SHdnZnFlUYrZqmKZ5mnBiCTVKvB6U5fwITKtoDRnQyk94JHf"
)
DARAJA_CONSUMER_SECRET = os.getenv(
    "DARAJA_CONSUMER_SECRET", "nXemrIigA3bHoTtAtlPvqIeCCAbfwfby8eIJS3vx6FFIA2f0C8egkEoAaLPMwj0y"
)
DARAJA_SHORTCODE = os.getenv("DARAJA_SHORTCODE", "174379")
DARAJA_PASSKEY = os.getenv(
    "DARAJA_PASSKEY",
    "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919",
)
DARAJA_CALLBACK_BASE = os.getenv(
    "DARAJA_CALLBACK_BASE", "https://onloan-mpesa.onrender.com"
).rstrip("/")

OTP_TTL_MINUTES = 5
MAX_OTP_ATTEMPTS = 5
MAX_PIN_ATTEMPTS = 5

TIER_LIMITS: Dict[int, float] = {
    1: 5000.0,
    2: 15000.0,
    3: 35000.0,
}
DEFAULT_FEE_RATE = 0.08
DAILY_PENALTY_RATE = 0.015
CBK_PENALTY_CAP_MULTIPLIER = 1.0  # Total penalties/fees capped at 100% of principal

app = FastAPI(title=APP_NAME, version="1.0.0")


# ---------------------------------------------------------------------------
# Time, Crypto & Validation Helpers
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def _hmac_digest(secret: str, message: str) -> str:
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_otp(phone: str, code: str) -> str:
    return _hmac_digest(OTP_PEPPER, f"{phone}:{code}")


def hash_pin(phone: str, pin: str) -> str:
    return _hmac_digest(PIN_PEPPER, f"{phone}:{pin}")


def normalize_ke_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw.strip() if ch.isdigit())
    if digits.startswith("0") and len(digits) == 10:
        return "254" + digits[1:]
    if digits.startswith("254") and len(digits) == 12:
        return digits
    if (digits.startswith("7") or digits.startswith("1")) and len(digits) == 9:
        return "254" + digits
    raise HTTPException(
        status_code=400,
        detail="Enter a valid Kenyan mobile number (e.g. 0712345678 or 254712345678).",
    )


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def require_admin(x_admin_pin: Optional[str]) -> None:
    if not x_admin_pin or not secrets.compare_digest(x_admin_pin.strip(), ADMIN_PASSCODE):
        raise HTTPException(status_code=401, detail="Admin lock active: invalid admin PIN.")


# ---------------------------------------------------------------------------
# Database Initialization
# ---------------------------------------------------------------------------

def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS otp_verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                otp_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS borrowers (
                phone TEXT PRIMARY KEY,
                full_name TEXT NOT NULL DEFAULT '',
                id_number TEXT UNIQUE,
                date_of_birth TEXT,
                county TEXT,
                phone_verified INTEGER NOT NULL DEFAULT 0,
                kyc_status TEXT NOT NULL DEFAULT 'PENDING',
                pin_hash TEXT,
                failed_pin_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                tier INTEGER NOT NULL DEFAULT 1,
                completed_loans INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                principal REAL NOT NULL,
                fee_remaining REAL NOT NULL,
                principal_remaining REAL NOT NULL,
                penalty_accrued REAL NOT NULL DEFAULT 0.0,
                penalty_remaining REAL NOT NULL DEFAULT 0.0,
                due_date TEXT NOT NULL,
                last_penalty_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                disbursement_ref TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (phone) REFERENCES borrowers(phone)
            );

            CREATE TABLE IF NOT EXISTS repayments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                loan_id INTEGER NOT NULL,
                phone TEXT NOT NULL,
                mpesa_receipt TEXT NOT NULL,
                amount REAL NOT NULL,
                paid_principal REAL NOT NULL,
                paid_fee REAL NOT NULL,
                paid_penalty REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (loan_id) REFERENCES loans(id)
            );

            CREATE TABLE IF NOT EXISTS suspense_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_ref TEXT NOT NULL,
                phone TEXT,
                mpesa_receipt TEXT NOT NULL,
                amount REAL NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
                resolved_loan_id INTEGER,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS mpesa_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_type TEXT NOT NULL,
                phone TEXT NOT NULL,
                amount REAL NOT NULL,
                reference TEXT NOT NULL,
                status TEXT NOT NULL,
                raw_response TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()


@app.on_event("startup")
def on_startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# Loan Math: Overdue Penalties & CBK Waterfall Allocation
# ---------------------------------------------------------------------------

def refresh_loan_penalties(conn: sqlite3.Connection, loan_row: sqlite3.Row) -> Dict[str, Any]:
    loan = dict(loan_row)
    if loan["status"] != "ACTIVE":
        return loan

    now = utc_now()
    due_dt = datetime.fromisoformat(loan["due_date"])
    last_pen_dt = datetime.fromisoformat(loan["last_penalty_date"])

    anchor = max(due_dt, last_pen_dt)
    if now > anchor:
        days_overdue = max(0, (now.date() - anchor.date()).days)
        if days_overdue > 0:
            principal = float(loan["principal"])
            current_fee = float(loan["fee_remaining"])
            current_penalty = float(loan["penalty_remaining"])
            max_allowed_charges = principal * CBK_PENALTY_CAP_MULTIPLIER
            remaining_cap = max(0.0, max_allowed_charges - (current_fee + current_penalty))

            raw_penalty = round(principal * DAILY_PENALTY_RATE * days_overdue, 2)
            added_penalty = round(min(raw_penalty, remaining_cap), 2)

            new_accrued = round(float(loan["penalty_accrued"]) + added_penalty, 2)
            new_remaining = round(current_penalty + added_penalty, 2)
            new_anchor = now.isoformat()

            conn.execute(
                """
                UPDATE loans
                SET penalty_accrued = ?, penalty_remaining = ?, last_penalty_date = ?
                WHERE id = ?
                """,
                (new_accrued, new_remaining, new_anchor, loan["id"]),
            )
            conn.commit()
            loan["penalty_accrued"] = new_accrued
            loan["penalty_remaining"] = new_remaining
            loan["last_penalty_date"] = new_anchor

    return loan


def apply_cbk_waterfall(
    principal_rem: float, fee_rem: float, penalty_rem: float, payment_amount: float
) -> Dict[str, float]:
    """
    CBK-compliant repayment waterfall:
    1. Principal first
    2. Fee second
    3. Penalty third
    """
    remaining_payment = round(float(payment_amount), 2)

    paid_principal = round(min(principal_rem, remaining_payment), 2)
    remaining_payment = round(remaining_payment - paid_principal, 2)

    paid_fee = round(min(fee_rem, remaining_payment), 2)
    remaining_payment = round(remaining_payment - paid_fee, 2)

    paid_penalty = round(min(penalty_rem, remaining_payment), 2)
    overpayment = round(max(0.0, remaining_payment - paid_penalty), 2)

    return {
        "paid_principal": paid_principal,
        "paid_fee": paid_fee,
        "paid_penalty": paid_penalty,
        "principal_remaining": round(max(0.0, principal_rem - paid_principal), 2),
        "fee_remaining": round(max(0.0, fee_rem - paid_fee), 2),
        "penalty_remaining": round(max(0.0, penalty_rem - paid_penalty), 2),
        "overpayment": overpayment,
    }


def recalculate_tier(conn: sqlite3.Connection, phone: str) -> int:
    row = conn.execute(
        "SELECT completed_loans, kyc_status FROM borrowers WHERE phone = ?", (phone,)
    ).fetchone()
    if not row or row["kyc_status"] != "VERIFIED":
        tier = 1
    elif row["completed_loans"] >= 4:
        tier = 3
    elif row["completed_loans"] >= 2:
        tier = 2
    else:
        tier = 1

    conn.execute(
        "UPDATE borrowers SET tier = ?, updated_at = ? WHERE phone = ?",
        (tier, utc_now_iso(), phone),
    )
    return tier


# ---------------------------------------------------------------------------
# Safaricom Daraja Sandbox Helpers (STK Push & B2C)
# ---------------------------------------------------------------------------

def get_daraja_token() -> str:
    auth_bytes = f"{DARAJA_CONSUMER_KEY}:{DARAJA_CONSUMER_SECRET}".encode("utf-8")
    basic_b64 = base64.b64encode(auth_bytes).decode("ascii")
    url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    req = Request(url, headers={"Authorization": f"Basic {basic_b64}"}, method="GET")
    with urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Daraja OAuth did not return an access_token.")
    return str(token)


def trigger_stk_push(phone: str, amount: float, account_ref: str) -> Dict[str, Any]:
    if SANDBOX_MODE and os.getenv("LIVE_DARAJA_CALLS", "false").lower() != "true":
        return {
            "ResponseCode": "0",
            "ResponseDescription": "Success. Request accepted for processing (sandbox simulated)",
            "MerchantRequestID": f"MR-{secrets.token_hex(6).upper()}",
            "CheckoutRequestID": f"ws_CO_{secrets.token_hex(8).upper()}",
            "AccountReference": account_ref,
            "Amount": amount,
        }

    token = get_daraja_token()
    timestamp = utc_now().strftime("%Y%m%d%H%M%S")
    password_raw = f"{DARAJA_SHORTCODE}{DARAJA_PASSKEY}{timestamp}".encode("utf-8")
    password_b64 = base64.b64encode(password_raw).decode("ascii")

    payload = {
        "BusinessShortCode": DARAJA_SHORTCODE,
        "Password": password_b64,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(round(amount)),
        "PartyA": phone,
        "PartyB": DARAJA_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": f"{DARAJA_CALLBACK_BASE}/api/mpesa/stk-callback",
        "AccountReference": account_ref,
        "TransactionDesc": "OnLoan Repayment",
    }
    req = Request(
        "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def trigger_b2c_disbursement(phone: str, amount: float, remarks: str = "OnLoan Disbursement") -> Dict[str, Any]:
    if SANDBOX_MODE and os.getenv("LIVE_DARAJA_CALLS", "false").lower() != "true":
        return {
            "ResponseCode": "0",
            "ResponseDescription": "Accept the service request successfully. (sandbox simulated)",
            "ConversationID": f"AG_{secrets.token_hex(8)}",
            "OriginatorConversationID": f"OC_{secrets.token_hex(8)}",
            "Amount": amount,
            "PartyB": phone,
            "Remarks": remarks,
        }

    token = get_daraja_token()
    payload = {
        "OriginatorConversationID": f"ONLOAN-{secrets.token_hex(8)}",
        "InitiatorName": os.getenv("DARAJA_INITIATOR", "testapi"),
        "SecurityCredential": os.getenv("DARAJA_SECURITY_CREDENTIAL", ""),
        "CommandID": "BusinessPayment",
        "Amount": int(round(amount)),
        "PartyA": DARAJA_SHORTCODE,
        "PartyB": phone,
        "Remarks": remarks,
        "QueueTimeOutURL": f"{DARAJA_CALLBACK_BASE}/api/mpesa/b2c-timeout",
        "ResultURL": f"{DARAJA_CALLBACK_BASE}/api/mpesa/b2c-result",
        "Occasion": "LoanDisbursement",
    }
    req = Request(
        "https://sandbox.safaricom.co.ke/mpesa/b2c/v3/paymentrequest",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Request / Response Schemas
# ---------------------------------------------------------------------------

class RequestOtpPayload(BaseModel):
    phone: str


class VerifyOtpPayload(BaseModel):
    phone: str
    otp: str = Field(..., min_length=6, max_length=6)


class SubmitKycPayload(BaseModel):
    phone: str
    full_name: str = Field(..., min_length=3, max_length=120)
    id_number: str = Field(..., min_length=7, max_length=15)
    date_of_birth: Optional[str] = None
    county: Optional[str] = None


class SetPinPayload(BaseModel):
    phone: str
    pin: str = Field(..., pattern=r"^\d{4}$")


class LoginPinPayload(BaseModel):
    phone: str
    pin: str = Field(..., pattern=r"^\d{4}$")


class LoanApplyPayload(BaseModel):
    phone: str
    pin: str = Field(..., pattern=r"^\d{4}$")
    amount: float = Field(..., gt=0)
    term_days: int = Field(default=14, ge=7, le=60)


class RepayPayload(BaseModel):
    phone: str
    amount: float = Field(..., gt=0)
    mpesa_receipt: Optional[str] = None
    use_stk: bool = True


class C2BSimulatePayload(BaseModel):
    account_ref: str
    amount: float = Field(..., gt=0)
    phone: Optional[str] = None
    mpesa_receipt: Optional[str] = None


class ResolveSuspensePayload(BaseModel):
    suspense_id: int
    target_phone: str
    action: str = Field(default="ALLOCATE", pattern="^(ALLOCATE|REJECT)$")


class KycReviewPayload(BaseModel):
    phone: str
    kyc_status: str = Field(..., pattern="^(VERIFIED|REJECTED|PENDING)$")


# ---------------------------------------------------------------------------
# Onboarding Routes: Registration OTP & KYC
# ---------------------------------------------------------------------------

@app.post("/api/onboarding/request-otp")
def request_otp(payload: RequestOtpPayload) -> Dict[str, Any]:
    phone = normalize_ke_phone(payload.phone)
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = utc_now()
    expires = now + timedelta(minutes=OTP_TTL_MINUTES)

    with get_conn() as conn:
        conn.execute(
            "UPDATE otp_verifications SET verified = -1 WHERE phone = ? AND verified = 0",
            (phone,),
        )
        conn.execute(
            """
            INSERT INTO otp_verifications (phone, otp_hash, expires_at, attempts, verified, created_at)
            VALUES (?, ?, ?, 0, 0, ?)
            """,
            (phone, hash_otp(phone, code), expires.isoformat(), now.isoformat()),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO borrowers (phone, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            (phone, now.isoformat(), now.isoformat()),
        )
        conn.commit()

    result: Dict[str, Any] = {
        "status": "otp_sent",
        "phone": phone,
        "expires_in_seconds": OTP_TTL_MINUTES * 60,
    }
    if SANDBOX_MODE:
        result["debug_otp"] = code
    return result


@app.post("/api/onboarding/verify-otp")
def verify_otp(payload: VerifyOtpPayload) -> Dict[str, Any]:
    phone = normalize_ke_phone(payload.phone)
    code = payload.otp.strip()
    now = utc_now()

    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, otp_hash, expires_at, attempts
            FROM otp_verifications
            WHERE phone = ? AND verified = 0
            ORDER BY id DESC LIMIT 1
            """,
            (phone,),
        ).fetchone()

        if not row:
            raise HTTPException(status_code=400, detail="No active OTP found. Request a new code.")

        if int(row["attempts"]) >= MAX_OTP_ATTEMPTS:
            raise HTTPException(status_code=429, detail="Too many failed OTP attempts. Request a new code.")

        if now > datetime.fromisoformat(row["expires_at"]):
            raise HTTPException(status_code=400, detail="OTP has expired. Request a new code.")

        if not secrets.compare_digest(hash_otp(phone, code), row["otp_hash"]):
            conn.execute(
                "UPDATE otp_verifications SET attempts = attempts + 1 WHERE id = ?",
                (row["id"],),
            )
            conn.commit()
            raise HTTPException(status_code=400, detail="Invalid OTP code.")

        conn.execute("UPDATE otp_verifications SET verified = 1 WHERE id = ?", (row["id"],))
        conn.execute(
            """
            INSERT INTO borrowers (phone, phone_verified, created_at, updated_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                phone_verified = 1,
                updated_at = excluded.updated_at
            """,
            (phone, now.isoformat(), now.isoformat()),
        )
        conn.commit()

    return {"status": "phone_verified", "phone": phone}


@app.post("/api/onboarding/submit-kyc")
def submit_kyc(payload: SubmitKycPayload) -> Dict[str, Any]:
    phone = normalize_ke_phone(payload.phone)
    id_clean = "".join(ch for ch in payload.id_number.strip() if ch.isalnum()).upper()
    full_name_clean = " ".join(payload.full_name.strip().split()).title()
    now_iso = utc_now_iso()

    with get_conn() as conn:
        borrower = conn.execute(
            "SELECT phone_verified FROM borrowers WHERE phone = ?", (phone,)
        ).fetchone()
        if not borrower or int(borrower["phone_verified"]) != 1:
            raise HTTPException(
                status_code=403, detail="Verify your phone number with OTP before submitting KYC."
            )

        duplicate = conn.execute(
            "SELECT phone FROM borrowers WHERE id_number = ? AND phone != ?",
            (id_clean, phone),
        ).fetchone()
        if duplicate:
            raise HTTPException(
                status_code=409, detail="National ID number is already linked to another account."
            )

        status = "VERIFIED" if len(id_clean) >= 7 and len(full_name_clean) >= 3 else "PENDING"
        conn.execute(
            """
            UPDATE borrowers
            SET full_name = ?, id_number = ?, date_of_birth = ?, county = ?, kyc_status = ?, updated_at = ?
            WHERE phone = ?
            """,
            (full_name_clean, id_clean, payload.date_of_birth, payload.county, status, now_iso, phone),
        )
        tier = recalculate_tier(conn, phone)
        conn.commit()

    return {
        "status": "kyc_saved",
        "phone": phone,
        "full_name": full_name_clean,
        "id_number": id_clean,
        "kyc_status": status,
        "tier": tier,
        "credit_limit": TIER_LIMITS[tier],
    }


# ---------------------------------------------------------------------------
# Borrower PIN Auth & Account Summary
# ---------------------------------------------------------------------------

@app.post("/api/auth/set-pin")
def set_pin(payload: SetPinPayload) -> Dict[str, Any]:
    phone = normalize_ke_phone(payload.phone)
    now_iso = utc_now_iso()

    with get_conn() as conn:
        borrower = conn.execute("SELECT * FROM borrowers WHERE phone = ?", (phone,)).fetchone()
        if not borrower or int(borrower["phone_verified"]) != 1:
            raise HTTPException(status_code=403, detail="Complete OTP verification first.")
        if not borrower["id_number"]:
            raise HTTPException(status_code=403, detail="Submit KYC details before setting your PIN.")

        conn.execute(
            """
            UPDATE borrowers
            SET pin_hash = ?, failed_pin_attempts = 0, locked_until = NULL, updated_at = ?
            WHERE phone = ?
            """,
            (hash_pin(phone, payload.pin), now_iso, phone),
        )
        conn.commit()

    return {"status": "pin_set", "phone": phone}


def _verify_borrower_pin(conn: sqlite3.Connection, phone: str, pin: str) -> sqlite3.Row:
    borrower = conn.execute("SELECT * FROM borrowers WHERE phone = ?", (phone,)).fetchone()
    if not borrower or not borrower["pin_hash"]:
        raise HTTPException(status_code=404, detail="Account or PIN not configured.")

    now = utc_now()
    if borrower["locked_until"] and now < datetime.fromisoformat(borrower["locked_until"]):
        raise HTTPException(
            status_code=423, detail="Account temporarily locked due to failed PIN attempts."
        )

    if not secrets.compare_digest(hash_pin(phone, pin), borrower["pin_hash"]):
        new_fails = int(borrower["failed_pin_attempts"]) + 1
        locked_until = (
            (now + timedelta(minutes=15)).isoformat() if new_fails >= MAX_PIN_ATTEMPTS else None
        )
        conn.execute(
            """
            UPDATE borrowers
            SET failed_pin_attempts = ?, locked_until = ?, updated_at = ?
            WHERE phone = ?
            """,
            (new_fails, locked_until, now.isoformat(), phone),
        )
        conn.commit()
        raise HTTPException(status_code=401, detail="Invalid 4-digit PIN.")

    conn.execute(
        """
        UPDATE borrowers
        SET failed_pin_attempts = 0, locked_until = NULL, updated_at = ?
        WHERE phone = ?
        """,
        (now.isoformat(), phone),
    )
    conn.commit()
    return borrower


@app.post("/api/auth/login")
def login_with_pin(payload: LoginPinPayload) -> Dict[str, Any]:
    phone = normalize_ke_phone(payload.phone)
    with get_conn() as conn:
        borrower = _verify_borrower_pin(conn, phone, payload.pin)
        tier = int(borrower["tier"])
        active_loan = conn.execute(
            "SELECT * FROM loans WHERE phone = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
            (phone,),
        ).fetchone()
        loan_data = refresh_loan_penalties(conn, active_loan) if active_loan else None

    return {
        "status": "authenticated",
        "phone": phone,
        "full_name": borrower["full_name"],
        "kyc_status": borrower["kyc_status"],
        "tier": tier,
        "credit_limit": TIER_LIMITS.get(tier, 5000.0),
        "active_loan": loan_data,
    }


# ---------------------------------------------------------------------------
# Loan Origination, Waterfall Repayment & C2B Suspense Queue
# ---------------------------------------------------------------------------

@app.post("/api/loans/apply")
def apply_loan(payload: LoanApplyPayload) -> Dict[str, Any]:
    phone = normalize_ke_phone(payload.phone)
    amount = round(float(payload.amount), 2)

    with get_conn() as conn:
        borrower = _verify_borrower_pin(conn, phone, payload.pin)

        if borrower["kyc_status"] != "VERIFIED":
            raise HTTPException(
                status_code=403,
                detail=f"KYC status is '{borrower['kyc_status']}'. Verified KYC is required before loan disbursement.",
            )

        tier = recalculate_tier(conn, phone)
        max_limit = TIER_LIMITS.get(tier, 5000.0)
        if amount > max_limit:
            raise HTTPException(
                status_code=400,
                detail=f"Requested KES {amount:,.2f} exceeds Tier {tier} limit of KES {max_limit:,.2f}.",
            )

        existing_active = conn.execute(
            "SELECT id FROM loans WHERE phone = ? AND status = 'ACTIVE'", (phone,)
        ).fetchone()
        if existing_active:
            raise HTTPException(
                status_code=409,
                detail="You already have an active loan. Clear it before taking a new loan.",
            )

        b2c_resp = trigger_b2c_disbursement(phone, amount)
        disb_ref = str(
            b2c_resp.get("ConversationID") or f"B2C-{secrets.token_hex(5).upper()}"
        )

        fee = round(amount * DEFAULT_FEE_RATE, 2)
        now = utc_now()
        due_date = (now + timedelta(days=payload.term_days)).isoformat()

        cur = conn.execute(
            """
            INSERT INTO loans (
                phone, principal, fee_remaining, principal_remaining,
                penalty_accrued, penalty_remaining, due_date, last_penalty_date,
                status, disbursement_ref, created_at
            )
            VALUES (?, ?, ?, ?, 0.0, 0.0, ?, ?, 'ACTIVE', ?, ?)
            """,
            (
                phone,
                amount,
                fee,
                amount,
                due_date,
                now.isoformat(),
                disb_ref,
                now.isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO mpesa_transactions (tx_type, phone, amount, reference, status, raw_response, created_at)
            VALUES ('B2C_DISBURSEMENT', ?, ?, ?, 'COMPLETED', ?, ?)
            """,
            (phone, amount, disb_ref, json.dumps(b2c_resp), now.isoformat()),
        )
        conn.commit()
        loan_id = cur.lastrowid

    return {
        "status": "disbursed",
        "loan_id": loan_id,
        "phone": phone,
        "principal": amount,
        "fee": fee,
        "due_date": due_date,
        "disbursement_ref": disb_ref,
        "daraja": b2c_resp,
    }


def _apply_repayment_to_loan(
    conn: sqlite3.Connection, loan_row: sqlite3.Row, amount: float, receipt: str
) -> Dict[str, Any]:
    loan = refresh_loan_penalties(conn, loan_row)
    alloc = apply_cbk_waterfall(
        principal_rem=float(loan["principal_remaining"]),
        fee_rem=float(loan["fee_remaining"]),
        penalty_rem=float(loan["penalty_remaining"]),
        payment_amount=amount,
    )

    now_iso = utc_now_iso()
    is_cleared = (
        alloc["principal_remaining"] <= 0.0
        and alloc["fee_remaining"] <= 0.0
        and alloc["penalty_remaining"] <= 0.0
    )
    new_status = "CLEARED" if is_cleared else "ACTIVE"

    conn.execute(
        """
        UPDATE loans
        SET principal_remaining = ?, fee_remaining = ?, penalty_remaining = ?, status = ?
        WHERE id = ?
        """,
        (
            alloc["principal_remaining"],
            alloc["fee_remaining"],
            alloc["penalty_remaining"],
            new_status,
            loan["id"],
        ),
    )
    conn.execute(
        """
        INSERT INTO repayments (
            loan_id, phone, mpesa_receipt, amount, paid_principal, paid_fee, paid_penalty, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            loan["id"],
            loan["phone"],
            receipt,
            amount,
            alloc["paid_principal"],
            alloc["paid_fee"],
            alloc["paid_penalty"],
            now_iso,
        ),
    )

    new_tier = int(
        conn.execute("SELECT tier FROM borrowers WHERE phone = ?", (loan["phone"],)).fetchone()["tier"]
    )
    if is_cleared:
        conn.execute(
            "UPDATE borrowers SET completed_loans = completed_loans + 1, updated_at = ? WHERE phone = ?",
            (now_iso, loan["phone"]),
        )
        new_tier = recalculate_tier(conn, loan["phone"])

    conn.commit()
    return {
        "loan_id": loan["id"],
        "receipt": receipt,
        "loan_status": new_status,
        "waterfall": alloc,
        "tier": new_tier,
    }


@app.post("/api/loans/repay")
def repay_loan(payload: RepayPayload) -> Dict[str, Any]:
    phone = normalize_ke_phone(payload.phone)
    amount = round(float(payload.amount), 2)
    receipt = (payload.mpesa_receipt or f"Q{secrets.token_hex(5).upper()}").strip().upper()

    with get_conn() as conn:
        active_loan = conn.execute(
            "SELECT * FROM loans WHERE phone = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
            (phone,),
        ).fetchone()
        if not active_loan:
            raise HTTPException(status_code=404, detail="No active loan found for this phone number.")

        stk_resp = trigger_stk_push(phone, amount, account_ref=f"ONLOAN-{active_loan['id']}") if payload.use_stk else None
        result = _apply_repayment_to_loan(conn, active_loan, amount, receipt)

    return {
        "status": "repayment_recorded",
        "phone": phone,
        "amount": amount,
        "stk_push": stk_resp,
        **result,
    }


@app.post("/api/mpesa/c2b-simulate")
def simulate_c2b_paybill(payload: C2BSimulatePayload) -> Dict[str, Any]:
    amount = round(float(payload.amount), 2)
    receipt = (payload.mpesa_receipt or f"C2B{secrets.token_hex(5).upper()}").strip().upper()
    account_ref = payload.account_ref.strip()
    now_iso = utc_now_iso()

    resolved_phone: Optional[str] = None
    if payload.phone:
        try:
            resolved_phone = normalize_ke_phone(payload.phone)
        except HTTPException:
            resolved_phone = None

    with get_conn() as conn:
        loan_row = None
        if account_ref.isdigit():
            loan_row = conn.execute(
                "SELECT * FROM loans WHERE id = ? AND status = 'ACTIVE'", (int(account_ref),)
            ).fetchone()
        if not loan_row and account_ref.upper().startswith("ONLOAN-"):
            suffix = account_ref.split("-", 1)[1]
            if suffix.isdigit():
                loan_row = conn.execute(
                    "SELECT * FROM loans WHERE id = ? AND status = 'ACTIVE'", (int(suffix),)
                ).fetchone()
        if not loan_row and resolved_phone:
            loan_row = conn.execute(
                "SELECT * FROM loans WHERE phone = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
                (resolved_phone,),
            ).fetchone()

        if loan_row:
            applied = _apply_repayment_to_loan(conn, loan_row, amount, receipt)
            return {"status": "matched_and_allocated", **applied}

        cur = conn.execute(
            """
            INSERT INTO suspense_payments (
                account_ref, phone, mpesa_receipt, amount, reason, status, created_at
            )
            VALUES (?, ?, ?, ?, 'NO_ACTIVE_LOAN_OR_UNMATCHED_REF', 'PENDING_REVIEW', ?)
            """,
            (account_ref, resolved_phone, receipt, amount, now_iso),
        )
        conn.commit()

    return {
        "status": "queued_in_suspense",
        "suspense_id": cur.lastrowid,
        "account_ref": account_ref,
        "mpesa_receipt": receipt,
        "amount": amount,
    }


# ---------------------------------------------------------------------------
# Admin Lock Routes: Suspense Queue & KYC Review
# ---------------------------------------------------------------------------

@app.get("/api/admin/suspense")
def list_suspense(x_admin_pin: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    require_admin(x_admin_pin)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM suspense_payments WHERE status = 'PENDING_REVIEW' ORDER BY id DESC"
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/admin/suspense/resolve")
def resolve_suspense(
    payload: ResolveSuspensePayload, x_admin_pin: Optional[str] = Header(default=None)
) -> Dict[str, Any]:
    require_admin(x_admin_pin)
    target_phone = normalize_ke_phone(payload.target_phone)
    now_iso = utc_now_iso()

    with get_conn() as conn:
        susp = conn.execute(
            "SELECT * FROM suspense_payments WHERE id = ?", (payload.suspense_id,)
        ).fetchone()
        if not susp:
            raise HTTPException(status_code=404, detail="Suspense record not found.")
        if susp["status"] != "PENDING_REVIEW":
            raise HTTPException(status_code=409, detail="Suspense record already processed.")

        if payload.action == "REJECT":
            conn.execute(
                "UPDATE suspense_payments SET status = 'REJECTED', resolved_at = ? WHERE id = ?",
                (now_iso, susp["id"]),
            )
            conn.commit()
            return {"status": "rejected", "suspense_id": susp["id"]}

        active_loan = conn.execute(
            "SELECT * FROM loans WHERE phone = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
            (target_phone,),
        ).fetchone()
        if not active_loan:
            raise HTTPException(status_code=404, detail="Target borrower has no active loan.")

        applied = _apply_repayment_to_loan(
            conn, active_loan, float(susp["amount"]), str(susp["mpesa_receipt"])
        )
        conn.execute(
            """
            UPDATE suspense_payments
            SET status = 'RESOLVED', resolved_loan_id = ?, resolved_at = ?
            WHERE id = ?
            """,
            (active_loan["id"], now_iso, susp["id"]),
        )
        conn.commit()

    return {"status": "resolved", "suspense_id": susp["id"], **applied}


@app.post("/api/admin/kyc/review")
def review_kyc(
    payload: KycReviewPayload, x_admin_pin: Optional[str] = Header(default=None)
) -> Dict[str, Any]:
    require_admin(x_admin_pin)
    phone = normalize_ke_phone(payload.phone)
    with get_conn() as conn:
        borrower = conn.execute("SELECT phone FROM borrowers WHERE phone = ?", (phone,)).fetchone()
        if not borrower:
            raise HTTPException(status_code=404, detail="Borrower not found.")
        conn.execute(
            "UPDATE borrowers SET kyc_status = ?, updated_at = ? WHERE phone = ?",
            (payload.kyc_status, utc_now_iso(), phone),
        )
        tier = recalculate_tier(conn, phone)
        conn.commit()

    return {
        "status": "kyc_updated",
        "phone": phone,
        "kyc_status": payload.kyc_status,
        "tier": tier,
    }


@app.post("/api/mpesa/stk-callback")
def stk_callback(body: Dict[str, Any]) -> Dict[str, str]:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO mpesa_transactions (tx_type, phone, amount, reference, status, raw_response, created_at)
            VALUES ('STK_CALLBACK', '254000000000', 0.0, 'STK', 'RECEIVED', ?, ?)
            """,
            (json.dumps(body), utc_now_iso()),
        )
        conn.commit()
    return {"ResultCode": "0", "ResultDesc": "Accepted"}


# ---------------------------------------------------------------------------
# Store-Ready PWA Endpoints (/manifest.json, /sw.js, /icon.svg, /)
# ---------------------------------------------------------------------------

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
        "categories": ["finance", "productivity"],
        "icons": [
            {
                "src": "/icon.svg",
                "sizes": "192x192 512x512",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    }


@app.get("/sw.js")
def service_worker() -> Response:
    js_code = """
    const CACHE_NAME = 'onloan-pwa-v1';
    self.addEventListener('install', (event) => {
        event.waitUntil(
            caches.open(CACHE_NAME).then((cache) => cache.addAll(['/', '/manifest.json', '/icon.svg']))
        );
        self.skipWaiting();
    });
    self.addEventListener('activate', (event) => {
        event.waitUntil(self.clients.claim());
    });
    self.addEventListener('fetch', (event) => {
        event.respondWith(
            fetch(event.request).catch(() => caches.match(event.request))
        );
    });
    """
    return Response(content=js_code.strip(), media_type="application/javascript")


@app.get("/icon.svg")
def app_icon() -> Response:
    svg = """
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
      <rect width="512" height="512" rx="96" fill="#0f172a"/>
      <circle cx="256" cy="256" r="168" fill="#16a34a"/>
      <text x="256" y="286" text-anchor="middle" fill="#ffffff"
            font-family="Arial, sans-serif" font-size="112" font-weight="bold">KES</text>
    </svg>
    """.strip()
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
def index_page() -> str:
    return """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <meta name="theme-color" content="#1a73e8" />
      <link rel="manifest" href="/manifest.json" />
      <title>OnLoan — Instant M-Pesa Credit</title>
      <style>
        body { font-family: system-ui, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }
        .card { max-width: 520px; margin: 0 auto 16px; background: #1e293b; border-radius: 12px; padding: 18px; }
        input, button { width: 100%; box-sizing: border-box; padding: 10px 12px; margin-top: 8px; border-radius: 8px; border: 1px solid #334155; }
        input { background: #0f172a; color: #f8fafc; }
        button { background: #1a73e8; color: #fff; font-weight: 600; cursor: pointer; border: none; }
        pre { background: #0f172a; padding: 12px; border-radius: 8px; overflow-x: auto; font-size: 12px; color: #86efac; }
      </style>
    </head>
    <body>
      <div class="card">
        <h2>OnLoan Onboarding (OTP + KYC + PIN)</h2>
        <input id="phone" placeholder="Phone (e.g. 0712345678)" />
        <button onclick="reqOtp()">1. Send Registration OTP</button>
        <input id="otp" placeholder="6-digit OTP" />
        <button onclick="verOtp()">2. Verify OTP</button>
        <input id="fullName" placeholder="Full Name (as on National ID)" />
        <input id="idNumber" placeholder="National ID Number" />
        <button onclick="subKyc()">3. Submit KYC</button>
        <input id="pin" type="password" maxlength="4" placeholder="4-digit PIN" />
        <button onclick="setPin()">4. Set 4-Digit PIN</button>
      </div>
      <div class="card">
        <h3>Response Log</h3>
        <pre id="out">Ready.</pre>
      </div>
      <script>
        if ('serviceWorker' in navigator) {
          navigator.serviceWorker.register('/sw.js').catch(() => {});
        }
        const out = (d) => document.getElementById('out').textContent = JSON.stringify(d, null, 2);
        const val = (id) => document.getElementById(id).value;
        async function post(url, body) {
          const r = await fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
          out(await r.json());
        }
        const reqOtp = () => post('/api/onboarding/request-otp', { phone: val('phone') });
        const verOtp = () => post('/api/onboarding/verify-otp', { phone: val('phone'), otp: val('otp') });
        const subKyc = () => post('/api/onboarding/submit-kyc', { phone: val('phone'), full_name: val('fullName'), id_number: val('idNumber') });
        const setPin = () => post('/api/auth/set-pin', { phone: val('phone'), pin: val('pin') });
      </script>
    </body>
    </html>
    """