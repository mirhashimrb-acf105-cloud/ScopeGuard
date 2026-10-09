# =============================================================================
# ScopeGuard AI — app.py  (v3 — Premium UI/UX Overhaul)
# B2B SaaS Scope Creep Prevention Tool
# Python 3.8 compatible
# =============================================================================

import io
import json
import os
import pickle
import re
from typing import Dict, List, Optional

import streamlit as st
import pdfplumber
import requests
from thefuzz import fuzz
from fpdf import FPDF

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ScopeGuard AI",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# =============================================================================
# CONSTANTS
# =============================================================================
DEPARTMENTS = [
    "UI/UX Design",
    "Frontend Development",
    "Backend Development",
    "Project Management",
]

DEFAULT_DEPT_RATES = {
    "UI/UX Design": 75,
    "Frontend Development": 85,
    "Backend Development": 95,
    "Project Management": 65,
}

VALID_STATUSES = {
    "🟢 IN-SCOPE",
    "🔴 OUT-OF-SCOPE",
    "🟡 PARTIALLY IN-SCOPE",
    "⚪ UNADDRESSED IN SOW",
}

REQUIRED_KEYS = {"status", "exact_verbatim_quote", "reasoning", "technical_tasks_list"}

STATUS_STYLES = {
    "🟢 IN-SCOPE":           ("#22c55e", "#052e16", "#bbf7d0"),
    "🔴 OUT-OF-SCOPE":       ("#ef4444", "#2d0707", "#fecaca"),
    "🟡 PARTIALLY IN-SCOPE": ("#f59e0b", "#2d1a00", "#fde68a"),
    "⚪ UNADDRESSED IN SOW": ("#94a3b8", "#0f172a", "#cbd5e1"),
}

# =============================================================================
# DISK PERSISTENCE — survives Ctrl+R / browser refresh
# =============================================================================
_SAVE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "_session_cache.pkl")

_PERSIST_PREFIXES = ("editable_tasks_", "rate_")
_PERSIST_KEYS     = ["app_stage", "sow_text", "sow_filename", "messages"]


def _save_session():
    """Write important session state to disk so Ctrl+R doesn't lose work."""
    try:
        data = {k: st.session_state.get(k) for k in _PERSIST_KEYS}
        for k, v in st.session_state.items():
            if any(k.startswith(p) for p in _PERSIST_PREFIXES):
                data[k] = v
        os.makedirs(os.path.dirname(_SAVE_FILE), exist_ok=True)
        with open(_SAVE_FILE, "wb") as _f:
            pickle.dump(data, _f)
    except Exception:
        pass  # never crash the app because of a save failure


def _load_session():
    """Restore session from disk on a fresh browser session."""
    if "_session_loaded" in st.session_state:
        return  # already restored this run
    st.session_state["_session_loaded"] = True
    if not os.path.exists(_SAVE_FILE):
        return
    try:
        with open(_SAVE_FILE, "rb") as _f:
            data = pickle.load(_f)
        for k, v in data.items():
            st.session_state[k] = v
    except Exception:
        pass  # corrupted cache — start fresh silently


# =============================================================================
# SESSION STATE — initialised once, survives Ctrl+R
# =============================================================================
DEFAULTS = {
    "app_stage": 0,
    "sow_text": None,
    "sow_filename": None,
    "messages": [],
}

# Restore from disk FIRST (before setting defaults)
_load_session()

# Then fill in anything still missing with defaults
for _key, _default in DEFAULTS.items():
    if _key not in st.session_state:
        st.session_state[_key] = _default

for _dept, _rate in DEFAULT_DEPT_RATES.items():
    _rk = "rate_" + _dept.replace("/", "_").replace(" ", "_")
    if _rk not in st.session_state:
        st.session_state[_rk] = _rate


# =============================================================================
# UTILITY HELPERS
# =============================================================================
def get_dept_rate(dept: str) -> int:
    rk = "rate_" + dept.replace("/", "_").replace(" ", "_")
    return int(st.session_state.get(rk, DEFAULT_DEPT_RATES.get(dept, 75)))


def full_reset():
    for k, v in DEFAULTS.items():
        st.session_state[k] = [] if isinstance(v, list) else v
    for k in list(st.session_state.keys()):
        if any(k.startswith(p) for p in ("dept_", "hrs_", "name_", "gen_pdf_", "editable_tasks_")):
            del st.session_state[k]
    # Also wipe the disk cache so Ctrl+R starts fresh
    try:
        if os.path.exists(_SAVE_FILE):
            os.remove(_SAVE_FILE)
    except Exception:
        pass


def _calc_grand_total() -> float:
    """Sum all billable costs across every message that has task estimates."""
    total = 0.0
    for idx, msg in enumerate(st.session_state.get("messages", [])):
        res = msg.get("analysis_result")
        if not res:
            continue
        status = res.get("status", "")
        if status == "🟢 IN-SCOPE":
            continue
        et_key = f"editable_tasks_{idx}"
        tasks = st.session_state.get(et_key, [])
        for t in tasks:
            if t.get("denied"):
                continue
            rate = get_dept_rate(t.get("department", "Backend Development"))
            total += float(t.get("hours", 0)) * rate
    return total


def _get_invoice_items() -> list:
    """Return all accepted (non-denied, non-in-scope) task line items across all messages."""
    items = []
    for idx, msg in enumerate(st.session_state.get("messages", [])):
        res = msg.get("analysis_result")
        if not res:
            continue
        status = res.get("status", "")
        if status == "🟢 IN-SCOPE":
            continue
        et_key = f"editable_tasks_{idx}"
        tasks = st.session_state.get(et_key, [])
        accepted = [t for t in tasks if not t.get("denied") and float(t.get("hours", 0)) > 0]
        if accepted:
            items.append({
                "request": msg.get("user_request", f"Request #{idx}"),
                "tasks": accepted,
                "msg_idx": idx,
            })
    return items


# =============================================================================
# INPUT VALIDATOR
# =============================================================================
def is_valid_change_request(text: str) -> bool:
    import unicodedata as _ud

    stripped = text.strip()
    if len(stripped.replace(" ", "")) < 3:
        return False

    alpha_only = "".join(ch for ch in stripped if _ud.category(ch).startswith("L"))
    if not alpha_only:
        return False

    _AZ_MAP = str.maketrans({
        "\u015f": "s", "\u015e": "s",
        "\u0131": "i", "\u0130": "i",
        "\u0259": "e", "\u018f": "e",
        "\u011f": "g", "\u011e": "g",
        "\u00f6": "o", "\u00d6": "o",
        "\u00fc": "u", "\u00dc": "u",
        "\u00e7": "c", "\u00c7": "c",
        "\u00e2": "a", "\u00ee": "i", "\u00fb": "u",
    })
    transliterated = stripped.lower().translate(_AZ_MAP)
    nfkd = _ud.normalize("NFKD", transliterated)
    ascii_folded = "".join(c for c in nfkd if not _ud.combining(c))
    normalised = re.sub(r"[^\w\s]", "", ascii_folded, flags=re.UNICODE)
    normalised = re.sub(r"\s+", " ", normalised).strip()
    tokens = normalised.split()

    _SOCIAL = {
        "hi", "hello", "hey", "howdy", "yo", "sup", "there", "everyone",
        "guys", "all", "folks", "ok", "okay", "lol", "lmao", "haha", "hehe",
        "thx", "ty", "cya", "nvm", "idk", "asdf", "qwerty",
        "salam", "salamlar", "olsun", "yaxsilara", "herkese",
        "necesen", "necesiz", "nece", "xos", "xosh", "geldin", "geldiniz",
        "gelmisiniz", "gunaydin", "sabah", "xeyir", "sagol", "teshekkur",
        "teshekkurler", "beli", "xeyr", "yox", "gorusenedek", "hoyda", "hoy",
        "merhaba", "selam", "nasilsin", "nasil", "kolay", "gelsin", "iyi", "gunler",
    }
    if tokens and all(tok in _SOCIAL for tok in tokens):
        return False

    _SINGLE_NOISE = {
        "test", "testing", "test123", "asd", "yes", "no", "nope", "yep",
        "sure", "thanks", "thank", "bye", "goodbye", "nothing", "nevermind",
    }
    if len(tokens) == 1 and tokens[0] in _SINGLE_NOISE:
        return False

    return True


# =============================================================================
# STEP 1: PDF EXTRACTION
# =============================================================================
@st.cache_data
def extract_pdf_text(uploaded_file_bytes: bytes) -> str:
    text_parts = []
    with pdfplumber.open(io.BytesIO(uploaded_file_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n".join(text_parts)


# =============================================================================
# STEP 2: JSON HELPER
# =============================================================================
def clean_json(raw_text: str) -> Optional[dict]:
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        st.error(f"❌ **JSON Parsing Error:** `{e}`\n\nRaw response:\n```\n{raw_text}\n```")
        return None


# =============================================================================
# STEP 3: GEMINI SYSTEM PROMPT
# =============================================================================
SYSTEM_PROMPT = (
    "You are a strict B2B Contract Auditor AI. Your only job is to analyse whether "
    "a client's new request is covered by an existing Statement of Work (SOW).\n\n"
    "RULES YOU MUST FOLLOW:\n"
    "1. You NEVER estimate prices, costs, or hours. That is not your role.\n"
    "2. You MUST classify the request into EXACTLY ONE of these four states:\n"
    "   - \U0001f7e2 IN-SCOPE\n"
    "   - \U0001f534 OUT-OF-SCOPE\n"
    "   - \U0001f7e1 PARTIALLY IN-SCOPE\n"
    "   - \u26aa UNADDRESSED IN SOW\n"
    "3. You MUST respond with ONLY a single valid JSON object \u2014 no prose outside the JSON.\n"
    "4. The JSON MUST contain exactly these four keys:\n"
    "   - \"status\": one of the four exact strings above (with emoji)\n"
    "   - \"exact_verbatim_quote\": the exact word-for-word sentence(s) from the SOW that "
    "support your classification. Set to \"\" if status is \u26aa UNADDRESSED IN SOW.\n"
    "   - \"reasoning\": a concise 2-4 sentence explanation of your decision.\n"
    "   - \"technical_tasks_list\": a JSON array of objects. Each object MUST have:\n"
    "       - \"task\": string describing one discrete technical task\n"
    "       - \"department\": the most appropriate department from this list ONLY: "
    "[\"UI/UX Design\", \"Frontend Development\", \"Backend Development\", "
    "\"Project Management\"]\n\n"
    "REQUIRED OUTPUT FORMAT (no other text):\n"
    "{\n"
    "  \"status\": \"\U0001f7e2 IN-SCOPE\",\n"
    "  \"exact_verbatim_quote\": \"The exact text from the SOW...\",\n"
    "  \"reasoning\": \"Your reasoning here.\",\n"
    "  \"technical_tasks_list\": [\n"
    "    {\"task\": \"Design new dashboard screens\", \"department\": \"UI/UX Design\"},\n"
    "    {\"task\": \"Implement REST API for reports\", \"department\": \"Backend Development\"}\n"
    "  ]\n"
    "}"
)


# =============================================================================
# STEP 4: GEMINI REST CALL
# =============================================================================
def analyze_with_gemini(sow_text: str, client_request: str) -> str:
    _api_key = _get_gemini_api_key()
    if not _api_key:
        raise RuntimeError("configuration")

    full_prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"=== STATEMENT OF WORK (SOW) ===\n{sow_text}\n\n"
        f"=== CLIENT'S NEW REQUEST ===\n{client_request}\n\n"
        "Analyse whether the client request is covered by the SOW above "
        "and respond with the JSON object."
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={_api_key}"
    payload = {"contents": [{"parts": [{"text": full_prompt}]}]}

    resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=180)
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


# =============================================================================
# STEP 5: FUZZY GUARDRAIL
# =============================================================================
def verify_quote(quote: str, original_text: str) -> int:
    return fuzz.partial_ratio(quote.lower(), original_text.lower())


# =============================================================================
# STEP 6: PDF GENERATION
# =============================================================================
def _clean_pdf_text(text: str) -> str:
    return (text.replace("\u2014", "-").replace("\u2019", "'")
            .replace("\u201c", '"').replace("\u201d", '"')
            .replace("\u2013", "-").replace("\u2026", "..."))


def generate_change_order_pdf(task_data: List[Dict], total_cost: float, client_request: str) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    client_request = _clean_pdf_text(client_request)

    pdf.set_font("Helvetica", "B", 20)
    pdf.set_fill_color(10, 20, 60)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 14, "ScopeGuard AI  -  Change Order", ln=True, align="C", fill=True)
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(0, 6, "Generated by ScopeGuard AI  |  Requires formal countersignature before work commences", ln=True, align="C")
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(10, 20, 60)
    pdf.cell(0, 8, "Client Change Request", ln=True)
    pdf.set_draw_color(10, 20, 60)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(40, 40, 40)
    pdf.multi_cell(0, 6, client_request)
    pdf.ln(5)

    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(10, 20, 60)
    pdf.cell(0, 8, "Itemised Task Breakdown", ln=True)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(210, 220, 245)
    pdf.set_text_color(10, 20, 60)
    pdf.cell(62, 8, "Task Description", border=1, fill=True)
    pdf.cell(42, 8, "Department", border=1, fill=True)
    pdf.cell(18, 8, "Hours", border=1, fill=True, align="C")
    pdf.cell(22, 8, "Rate/hr", border=1, fill=True, align="C")
    pdf.cell(46, 8, "Line Cost (USD)", border=1, fill=True, align="R")
    pdf.ln()

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(40, 40, 40)
    alt = False
    for td in task_data:
        r, g, b = (245, 247, 255) if alt else (255, 255, 255)
        pdf.set_fill_color(r, g, b)
        clean_task = _clean_pdf_text(td["task"])
        task_label = (clean_task[:42] + "...") if len(clean_task) > 42 else clean_task
        pdf.cell(62, 7, task_label, border=1, fill=alt)
        clean_dept = _clean_pdf_text(td["department"])
        dept_label = (clean_dept[:22] + "...") if len(clean_dept) > 22 else clean_dept
        pdf.cell(42, 7, dept_label, border=1, fill=alt)
        pdf.cell(18, 7, str(round(td["hours"], 1)), border=1, fill=alt, align="C")
        pdf.cell(22, 7, f"${td['rate']:,}", border=1, fill=alt, align="C")
        pdf.cell(46, 7, f"${td['line_cost']:,.2f}", border=1, fill=alt, align="R")
        pdf.ln()
        alt = not alt

    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(10, 20, 60)
    pdf.set_text_color(255, 255, 255)
    total_hours = sum(td["hours"] for td in task_data)
    pdf.cell(62 + 42 + 18 + 22, 9, f"Total Hours: {total_hours:.1f} hrs", fill=True)
    pdf.cell(46, 9, f"TOTAL: ${total_cost:,.2f}", border=1, fill=True, align="R")
    pdf.ln(10)

    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(130, 130, 130)
    pdf.multi_cell(0, 5, "This Change Order is computer-generated by ScopeGuard AI. It requires formal review and countersignature by both parties before work commences.")
    return bytes(pdf.output())


def generate_invoice_pdf(invoice_items: list, grand_total: float, sow_filename: str) -> bytes:
    """Generate a combined invoice PDF from all accepted tasks across all requests."""
    from datetime import datetime as _dt
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Header
    pdf.set_font("Helvetica", "B", 22)
    pdf.set_fill_color(20, 83, 45)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 16, "ScopeGuard AI  -  Invoice", ln=True, align="C", fill=True)
    pdf.ln(1)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(100, 100, 100)
    today = _dt.now().strftime("%B %d, %Y")
    pdf.cell(0, 6, f"Generated: {today}   |   SOW: {_clean_pdf_text(sow_filename or 'N/A')}", ln=True, align="C")
    pdf.ln(6)

    overall_line_num = 1
    for group in invoice_items:
        req_label = _clean_pdf_text(group["request"])
        req_label = (req_label[:90] + "...") if len(req_label) > 90 else req_label

        # Request group header
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_fill_color(220, 252, 231)
        pdf.set_text_color(20, 83, 45)
        pdf.cell(0, 8, f"  {req_label}", border=1, ln=True, fill=True)

        # Table header
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(240, 253, 244)
        pdf.set_text_color(20, 83, 45)
        pdf.cell(8,  7, "#",        border=1, fill=True, align="C")
        pdf.cell(68, 7, "Task",     border=1, fill=True)
        pdf.cell(38, 7, "Dept",     border=1, fill=True)
        pdf.cell(16, 7, "Hrs",      border=1, fill=True, align="C")
        pdf.cell(20, 7, "Rate/hr",  border=1, fill=True, align="C")
        pdf.cell(40, 7, "Cost",     border=1, fill=True, align="R")
        pdf.ln()

        # Rows
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(40, 40, 40)
        for t in group["tasks"]:
            rate = t.get("rate") or get_dept_rate(t.get("department", "Backend Development"))
            line_cost = float(t.get("hours", 0)) * rate
            task_lbl = _clean_pdf_text(t["name"] if "name" in t else t["task"])
            task_lbl = (task_lbl[:48] + "...") if len(task_lbl) > 48 else task_lbl
            dept_lbl = _clean_pdf_text(t.get("department", ""))
            dept_lbl = (dept_lbl[:22] + "...") if len(dept_lbl) > 22 else dept_lbl
            alt_fill = (overall_line_num % 2 == 0)
            if alt_fill:
                pdf.set_fill_color(248, 255, 250)
            pdf.cell(8,  6, str(overall_line_num), border=1, fill=alt_fill, align="C")
            pdf.cell(68, 6, task_lbl,               border=1, fill=alt_fill)
            pdf.cell(38, 6, dept_lbl,               border=1, fill=alt_fill)
            pdf.cell(16, 6, f"{t['hours']:.1f}",    border=1, fill=alt_fill, align="C")
            pdf.cell(20, 6, f"${rate:,}",            border=1, fill=alt_fill, align="C")
            pdf.cell(40, 6, f"${line_cost:,.2f}",   border=1, fill=alt_fill, align="R")
            pdf.ln()
            overall_line_num += 1
        pdf.ln(3)

    # Grand total row
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_fill_color(20, 83, 45)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(150, 10, "  TOTAL AMOUNT DUE", fill=True)
    pdf.cell(40,  10, f"${grand_total:,.2f}", fill=True, align="R")
    pdf.ln(12)

    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(130, 130, 130)
    pdf.multi_cell(0, 5, "This invoice is computer-generated by ScopeGuard AI and covers all accepted out-of-scope change requests. Denied items are excluded.")
    return bytes(pdf.output())


# =============================================================================
# API KEY
# =============================================================================
def _get_gemini_api_key() -> str:
    key = "AQ.Ab8RN6KI1JNrqFUHFi-sUKsMyfk9XcCJJsQCc0IlA-oDBHeOCw"
    if key and not key.startswith("AIza..."):
        return key
    return ""


# =============================================================================
# PREMIUM GLOBAL CSS — White / Green palette
# =============================================================================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

/* ── Base ── */
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

/* ── Sidebar: dark forest green ── */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #14532d 0%, #0f3d21 60%, #0a2a17 100%) !important;
    min-width: 340px !important;
    max-width: 400px !important;
}
.stApp {
    background-color: #f8fafc !important;
    background-image: radial-gradient(#cbd5e1 1px, transparent 1px) !important;
    background-size: 24px 24px !important;
}
/* Hide Streamlit Header & Deploy Button */
header[data-testid="stHeader"] { visibility: hidden !important; }
.stAppDeployButton { display: none !important; }

/* Sidebar Buttons */
section[data-testid="stSidebar"] .stButton > button,
section[data-testid="stSidebar"] [data-testid="stDownloadButton"] > button {
    background: #ffffff !important;
    color: #14532d !important;
    border: none !important;
    font-weight: 700 !important;
    box-shadow: 0 2px 6px rgba(0,0,0,0.15) !important;
}
section[data-testid="stSidebar"] .stButton > button *,
section[data-testid="stSidebar"] [data-testid="stDownloadButton"] > button * {
    color: #14532d !important;
}
section[data-testid="stSidebar"] .stButton > button:hover,
section[data-testid="stSidebar"] [data-testid="stDownloadButton"] > button:hover {
    background: #16a34a !important;
    color: #ffffff !important;
}
section[data-testid="stSidebar"] .stButton > button:hover *,
section[data-testid="stSidebar"] [data-testid="stDownloadButton"] > button:hover * {
    color: #ffffff !important;
}

section[data-testid="stSidebar"] * { color: #dcfce7 !important; }
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] select { color: #111827 !important; }

.sg-brand-row { display:flex; align-items:center; gap:12px; padding:18px 4px 12px 2px; }
.sg-icon-wrap { width:44px; height:44px; border-radius:12px; overflow:hidden; flex-shrink:0;
                background:#fff; display:flex; align-items:center; justify-content:center;
                box-shadow:0 2px 8px rgba(0,0,0,.3); }
.sg-icon-img  { width:100%; height:100%; object-fit:cover; }
.sg-brand-name { font-size:1rem; font-weight:800; color:#ffffff !important; letter-spacing:-.02em; }
.sg-brand-ai   { font-size:1rem; font-weight:800; color:#86efac !important; letter-spacing:-.02em; }
.sg-sidebar-sep { height:1px; margin:2px 0 12px; background:rgba(255,255,255,.12); }
.sg-sidebar-tagline { font-size:.62rem; color:#4ade80 !important; letter-spacing:.07em; margin:0 0 18px 2px; }
.sg-sidebar-footer  { font-size:.62rem; color:#166534 !important; text-align:center; line-height:1.9; }

/* ── Grand Total Banner ── */
.sg-total-banner {
    background: linear-gradient(135deg, #f0fdf4, #dcfce7);
    border: 1px solid #86efac;
    border-left: 4px solid #16a34a;
    border-radius: 12px;
    padding: 14px 20px;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.sg-total-label  { font-size:.8rem; font-weight:500; color:#15803d; letter-spacing:.04em; text-transform:uppercase; }
.sg-total-amount { font-size:1.6rem; font-weight:800; color:#15803d; letter-spacing:-.03em; }
.sg-total-sub    { font-size:.7rem; color:#4ade80; margin-top:2px; }

/* ── Welcome screen hero ── */
.sg-hero { text-align: center; padding: 60px 20px 40px; }
.sg-hero-icon { font-size: 4rem; margin-bottom: 16px; display:block; }
.sg-hero-title {
    font-size: 2.8rem; font-weight: 800;
    letter-spacing: -0.04em; margin: 0 0 12px;
    background: linear-gradient(135deg, #14532d, #16a34a, #22c55e);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
}
.sg-hero-sub { font-size: 1.1rem; color: #4b7a5e; max-width: 480px; margin: 0 auto 36px; line-height: 1.7; }

/* ── Feature cards ── */
.sg-cards { display:flex; gap:16px; justify-content:center; flex-wrap:wrap; margin-bottom:40px; }
.sg-card {
    background: #ffffff;
    border: 1.5px solid #bbf7d0;
    border-radius: 14px; padding: 20px 22px; width: 200px; text-align:left;
    box-shadow: 0 2px 12px rgba(22,163,74,.08);
    transition: border-color .2s, box-shadow .2s;
}
.sg-card:hover { border-color: #16a34a; box-shadow: 0 4px 20px rgba(22,163,74,.15); }
.sg-card-icon { font-size:1.4rem; margin-bottom:10px; }
.sg-card-title { font-size:.85rem; font-weight:700; color:#14532d; margin-bottom:4px; }
.sg-card-desc { font-size:.72rem; color:#4b7a5e; line-height:1.5; }

/* ── Upload screen ── */
.sg-upload-card {
    background: #fff;
    border: 2px dashed #86efac;
    border-radius: 16px; padding: 36px; text-align:center;
    transition: border-color .2s, background .2s;
}
.sg-upload-card:hover { border-color: #16a34a; background: #f0fdf4; }
.sg-upload-title { font-size:1.1rem; font-weight:700; color:#14532d; margin-bottom:6px; }
.sg-upload-hint  { font-size:.78rem; color:#4b7a5e; }

/* ── Status badge ── */
.sg-status-badge {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 8px 18px; border-radius: 100px;
    font-size: .85rem; font-weight: 700; letter-spacing: .01em;
    margin-bottom: 16px; border: 1.5px solid;
}

/* ── Analysis card ── */
.sg-analysis-card {
    background: #f8fffe;
    border: 1px solid #bbf7d0;
    border-radius: 14px; padding: 20px 22px; margin: 8px 0 12px;
    box-shadow: 0 1px 6px rgba(22,163,74,.06);
}
.sg-analysis-label {
    font-size: .68rem; font-weight: 600; color: #4b7a5e;
    text-transform: uppercase; letter-spacing: .08em; margin-bottom: 6px;
}
.sg-analysis-text { font-size: .88rem; color: #1f2937; line-height: 1.65; }
.sg-quote-box {
    background: #f0fdf4; border-left: 3px solid #16a34a;
    border-radius: 0 8px 8px 0; padding: 10px 14px;
    font-size: .82rem; color: #14532d; font-style: italic; line-height: 1.6;
    margin-top: 10px;
}

/* ── Task table header ── */
.sg-task-header {
    display:grid; grid-template-columns: 1fr 180px 90px 90px;
    gap:8px; padding:8px 12px;
    background:#f0fdf4; border-radius:8px 8px 0 0;
    font-size:.7rem; font-weight:600; color:#4b7a5e;
    text-transform:uppercase; letter-spacing:.07em;
    border:1px solid #bbf7d0; border-bottom:none;
    margin-top:12px;
}

/* ── Cost pill ── */
.sg-cost-pill {
    display:inline-block; background:#16a34a; color:#fff;
    border-radius:6px; padding:3px 10px;
    font-size:.82rem; font-weight:700;
}

/* ── Totals row ── */
.sg-totals-row {
    display:flex; justify-content:space-between; align-items:center;
    background: linear-gradient(135deg, #f0fdf4, #dcfce7);
    border:1px solid #86efac; border-radius:10px;
    padding:14px 18px; margin-top:14px;
}
.sg-totals-hrs  { font-size:.85rem; color:#4b7a5e; }
.sg-totals-cost { font-size:1.25rem; font-weight:800; color:#15803d; }

/* ── Main header ── */
.sg-main-header { text-align:center; padding:8px 0 4px; }
.sg-main-title  { font-size:1.6rem; font-weight:800; color:#14532d; letter-spacing:-0.03em; margin:4px 0 0; }
.sg-main-title span { color:#16a34a; }
.sg-header-rule {
    width:56%; max-width:320px; height:2px; margin:8px auto 6px;
    background:linear-gradient(90deg,transparent,#16a34a,transparent);
}
.sg-header-sub { font-size:.7rem; color:#4b7a5e; letter-spacing:.05em; margin-bottom:12px; }

/* ── In-scope banner ── */
.sg-inscope-banner {
    background: linear-gradient(135deg,#f0fdf4,#dcfce7);
    border: 1.5px solid #16a34a; border-radius: 12px;
    padding: 16px 20px; margin: 10px 0;
    display:flex; align-items:center; gap:12px;
}
.sg-inscope-icon { font-size:1.5rem; }
.sg-inscope-text { font-size:.88rem; color:#14532d; font-weight:500; line-height:1.5; }

/* ── Sidebar Dashboard ── */
.sg-dash-section-title {
    font-size:.6rem; font-weight:700; color:#4ade80 !important;
    text-transform:uppercase; letter-spacing:.1em;
    margin:16px 0 8px; padding:0;
}
.sg-stat-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:4px; }
.sg-stat-card {
    background:rgba(255,255,255,.08);
    border:1px solid rgba(255,255,255,.12); border-radius:10px;
    padding:10px 12px;
}
.sg-stat-card.blue  { border-left:3px solid #4ade80; }
.sg-stat-card.green { border-left:3px solid #86efac; }
.sg-stat-card.red   { border-left:3px solid #fca5a5; }
.sg-stat-card.amber { border-left:3px solid #fde68a; }
.sg-stat-val        { font-size:1.3rem; font-weight:800; color:#ffffff !important; line-height:1; margin-bottom:3px; }
.sg-stat-val.blue   { color:#4ade80 !important; }
.sg-stat-val.green  { color:#86efac !important; }
.sg-stat-val.red    { color:#fca5a5 !important; }
.sg-stat-val.amber  { color:#fde68a !important; }
.sg-stat-lbl        { font-size:.62rem; color:#86efac !important; line-height:1.3; }
.sg-sow-panel {
    background:rgba(255,255,255,.08);
    border:1px solid rgba(134,239,172,.25); border-radius:10px;
    padding:12px 14px; margin-bottom:8px;
}
.sg-sow-panel-label { font-size:.62rem; color:#4ade80 !important; text-transform:uppercase; letter-spacing:.08em; margin-bottom:4px; }
.sg-sow-panel-name  { font-size:.8rem; color:#dcfce7 !important; font-weight:600; display:flex; align-items:center; gap:6px; }
.sg-sow-dot { width:7px; height:7px; border-radius:50%; background:#4ade80;
              display:inline-block; animation:pulse-dot 2s ease-in-out infinite; flex-shrink:0; }
@keyframes pulse-dot { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.5;transform:scale(.8)} }
.sg-rate-row { display:flex; justify-content:space-between; align-items:center;
               padding:5px 0; border-bottom:1px solid rgba(255,255,255,.08); }
.sg-rate-row:last-child { border-bottom:none; }
.sg-rate-dept { font-size:.75rem; color:#86efac !important; }
.sg-rate-amt  { font-size:.75rem; font-weight:700; color:#4ade80 !important; }
/* ── Pricing ── */
.sg-pricing-section { max-width: 900px; margin: 40px auto 10px; text-align: center; }
.sg-pricing-title { font-size: 1.4rem; font-weight: 800; color: #14532d; margin-bottom: 24px; }
.sg-pricing-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }
.sg-pricing-card {
    background: #ffffff; border: 1px solid #bbf7d0; border-radius: 16px;
    padding: 24px; text-align: left; position: relative;
    box-shadow: 0 4px 20px rgba(22,163,74,.05);
    transition: transform .2s, box-shadow .2s;
}
.sg-pricing-card:hover { transform: translateY(-3px); box-shadow: 0 8px 30px rgba(22,163,74,.12); border-color:#16a34a; }
.sg-pricing-card.pro { border: 2px solid #16a34a; transform: scale(1.03); }
.sg-pricing-card.pro:hover { transform: scale(1.03) translateY(-3px); }
.sg-pricing-badge {
    position: absolute; top: -12px; left: 50%; transform: translateX(-50%);
    background: #16a34a; color: #fff; font-size: .65rem; font-weight: 700;
    padding: 4px 12px; border-radius: 20px; letter-spacing: .05em; text-transform: uppercase;
}
.sg-price-name { font-size: 1.1rem; font-weight: 700; color: #14532d; margin-bottom: 8px; }
.sg-price-amt { font-size: 2.2rem; font-weight: 800; color: #16a34a; line-height: 1; margin-bottom: 4px; }
.sg-price-mo { font-size: .85rem; color: #4b7a5e; font-weight: 500; }
.sg-price-desc { font-size: .75rem; color: #4b7a5e; margin: 12px 0 16px; line-height: 1.5; min-height: 34px; }
.sg-feature { font-size: .8rem; color: #1f2937; margin-bottom: 8px; display: flex; align-items: flex-start; gap: 8px; }
.sg-feature span { color: #16a34a; font-weight: bold; }
</style>
"""
, unsafe_allow_html=True)




# =============================================================================
# SIDEBAR - Full Dashboard
# =============================================================================
with st.sidebar:
    if os.path.exists("logo.png"):
        import base64 as _b64
        with open("logo.png", "rb") as _f:
            _logo_b64 = _b64.b64encode(_f.read()).decode()
        st.markdown(
            f'<div class="sg-brand-row">' 
            f'<div class="sg-icon-wrap"><img src="data:image/png;base64,{_logo_b64}" class="sg-icon-img"></div>'
            f'<div><span class="sg-brand-name">ScopeGuard</span><span class="sg-brand-ai"> AI</span></div>'
            f'</div><div class="sg-sidebar-sep"></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="sg-brand-row">'
            '<div class="sg-icon-wrap"><span style="font-size:1.2rem;">\U0001f6e1\ufe0f</span></div>'
            '<div><span class="sg-brand-name">ScopeGuard</span><span class="sg-brand-ai"> AI</span></div>'
            '</div><div class="sg-sidebar-sep"></div>',
            unsafe_allow_html=True,
        )
    st.markdown('<p class="sg-sidebar-tagline">SCOPE INTELLIGENCE FOR AGENCIES</p>', unsafe_allow_html=True)

    colA, colB = st.columns(2)
    with colA:
        if st.button("🏠 Home", use_container_width=True):
            st.session_state.app_stage = 0
            st.rerun()
    with colB:
        if st.button("✨ Reset", use_container_width=True):
            full_reset()
            st.session_state.app_stage = 0
            st.rerun()
    st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

    if st.session_state.sow_filename:
        st.markdown('<p class="sg-dash-section-title">\U0001f4c1 Active Project</p>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="sg-sow-panel">'
            f'<div class="sg-sow-panel-label">Statement of Work</div>'
            f'<div class="sg-sow-panel-name"><span class="sg-sow-dot"></span>{st.session_state.sow_filename}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    if st.session_state.app_stage == 2 and st.session_state.messages:
        _all_res = [m.get("analysis_result") for m in st.session_state.messages if m.get("analysis_result")]
        _n_total = len(_all_res)
        _n_in    = sum(1 for r in _all_res if r.get("status") == "\U0001f7e2 IN-SCOPE")
        _n_out   = sum(1 for r in _all_res if r.get("status") == "\U0001f534 OUT-OF-SCOPE")
        _n_par   = sum(1 for r in _all_res if r.get("status") == "\U0001f7e1 PARTIALLY IN-SCOPE")
        _grand   = _calc_grand_total()

        st.markdown('<p class="sg-dash-section-title">\U0001f4ca Project Dashboard</p>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="sg-stat-grid">'
            f'<div class="sg-stat-card blue"><div class="sg-stat-val blue">{_n_total}</div><div class="sg-stat-lbl">Total Requests</div></div>'
            f'<div class="sg-stat-card green"><div class="sg-stat-val green">{_n_in}</div><div class="sg-stat-lbl">Covered by SOW</div></div>'
            f'<div class="sg-stat-card red"><div class="sg-stat-val red">{_n_out}</div><div class="sg-stat-lbl">Out of Scope</div></div>'
            f'<div class="sg-stat-card amber"><div class="sg-stat-val amber">{_n_par}</div><div class="sg-stat-lbl">Partial Scope</div></div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div style="background:linear-gradient(135deg,#0c1f3d,#0f2a52);border:1px solid #1e3a5f;'
            f'border-left:4px solid #38bdf8;border-radius:10px;padding:12px 14px;margin-top:8px;">'
            f'<div style="font-size:.62rem;color:#475569;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px;">\U0001f4b0 Billable Total</div>'
            f'<div style="font-size:1.4rem;font-weight:800;color:#38bdf8;line-height:1;">${_grand:,.2f}</div>'
            f'<div style="font-size:.62rem;color:#334155;margin-top:3px;">USD - excl. in-scope work</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.markdown('<p class="sg-dash-section-title">\u26a1 Department Rates</p>', unsafe_allow_html=True)
    _rate_html = ""
    for _dept in DEPARTMENTS:
        _rk = "rate_" + _dept.replace("/", "_").replace(" ", "_")
        _rv = st.session_state.get(_rk, DEFAULT_DEPT_RATES.get(_dept, 75))
        _short = _dept.split(" ")[0]
        _rate_html += (
            f'<div class="sg-rate-row">'
            f'<span class="sg-rate-dept">{_short}</span>'
            f'<span class="sg-rate-amt">${_rv}/hr</span>'
            f'</div>'
        )
    st.markdown(
        f'<div style="background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:8px 12px;margin-bottom:8px;">'
        f'{_rate_html}</div>',
        unsafe_allow_html=True,
    )
    with st.expander("\u270f\ufe0f Edit Rates", expanded=False):
        for _dept in DEPARTMENTS:
            _rk = "rate_" + _dept.replace("/", "_").replace(" ", "_")
            st.number_input(_dept, min_value=10, max_value=999, step=5, key=_rk)

    # ── Invoice Section ──
    _inv_items = _get_invoice_items()
    if _inv_items:
        st.markdown('<p class="sg-dash-section-title">🧾 Invoice</p>', unsafe_allow_html=True)
        _inv_total = 0.0
        _inv_html = ''
        for group in _inv_items:
            short_req = group["request"]
            short_req = (short_req[:38] + "…") if len(short_req) > 38 else short_req
            _inv_html += (
                f'<div style="font-size:.65rem;color:#86efac;font-weight:700;'
                f'text-transform:uppercase;letter-spacing:.06em;margin:10px 0 3px;">'
                f'📝 {short_req}</div>'
            )
            for t in group["tasks"]:
                rate = get_dept_rate(t.get("department", "Backend Development"))
                lc = float(t.get("hours", 0)) * rate
                _inv_total += lc
                tname = t.get("name") or t.get("task", "")
                tname = (tname[:30] + "…") if len(tname) > 30 else tname
                _inv_html += (
                    f'<div style="display:flex;justify-content:space-between;align-items:center;'
                    f'padding:4px 6px;border-bottom:1px solid rgba(255,255,255,.06);font-size:.72rem;">'
                    f'<span style="color:#dcfce7;">{tname}</span>'
                    f'<span style="color:#4ade80;font-weight:700;white-space:nowrap;margin-left:6px;">${lc:,.0f}</span>'
                    f'</div>'
                )
        _inv_html += (
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'background:rgba(74,222,128,.15);border-radius:6px;padding:7px 8px;margin-top:8px;">'
            f'<span style="color:#86efac;font-weight:800;font-size:.8rem;">TOTAL</span>'
            f'<span style="color:#4ade80;font-weight:800;font-size:1rem;">${_inv_total:,.2f}</span>'
            f'</div>'
        )
        st.markdown(
            f'<div style="background:rgba(0,0,0,.2);border:1px solid rgba(255,255,255,.1);'
            f'border-radius:10px;padding:8px 10px;">{_inv_html}</div>',
            unsafe_allow_html=True
        )
        _inv_pdf_bytes = generate_invoice_pdf(
            invoice_items=_inv_items,
            grand_total=_inv_total,
            sow_filename=st.session_state.get("sow_filename", ""),
        )
        st.download_button(
            label="⬇️ Download Invoice PDF",
            data=_inv_pdf_bytes,
            file_name="ScopeGuard_Invoice.pdf",
            mime="application/pdf",
            key="sidebar_invoice_dl",
            use_container_width=True,
        )

    st.divider()
    st.markdown(
        '<p class="sg-sidebar-footer">Built for the 2026 Hackathon<br>'
        '<span style="color:#38bdf8;">\u25cf</span> Powered by Gemini 3.5 Flash Lite</p>',
        unsafe_allow_html=True,
    )





# =============================================================================
# GRAND TOTAL BANNER (always visible in chat stage)
# =============================================================================
if st.session_state.app_stage == 2:
    grand_total = _calc_grand_total()
    num_billable = sum(
        1 for i, m in enumerate(st.session_state.get("messages", []))
        if m.get("analysis_result") and m["analysis_result"].get("status") != "🟢 IN-SCOPE"
        and any(float(t.get("hours", 0)) > 0 for t in st.session_state.get(f"editable_tasks_{i}", []))
    )
    col_banner, col_sow = st.columns([3, 1])
    with col_banner:
        st.markdown(
            f'<div class="sg-total-banner">'
            f'<div><div class="sg-total-label">💰 Project Running Total</div>'
            f'<div class="sg-total-sub">{num_billable} billable request{"s" if num_billable != 1 else ""} · updates live</div></div>'
            f'<div style="text-align:right;">'
            f'<div class="sg-total-amount">${grand_total:,.2f}</div>'
            f'<div class="sg-total-sub">USD · excl. in-scope work</div>'
            f'</div></div>',
            unsafe_allow_html=True,
        )
    with col_sow:
        if st.session_state.sow_filename:
            st.markdown(
                f'<div style="background:#0f172a;border:1px solid #1e3a5f;border-radius:12px;'
                f'padding:12px 16px;height:100%;display:flex;flex-direction:column;justify-content:center;">'
                f'<div style="font-size:.65rem;color:#475569;text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px;">Active SOW</div>'
                f'<div style="font-size:.8rem;color:#94a3b8;font-weight:500;">📎 {st.session_state.sow_filename}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )


# =============================================================================
# SCREEN ROUTING
# =============================================================================
_stage = st.session_state.app_stage

# ── WELCOME ──────────────────────────────────────────────────────────────────
if _stage == 0:
    st.markdown("<br><br>", unsafe_allow_html=True)
            
    st.markdown(
        '<div class="sg-hero">'
        '<h1 class="sg-hero-title">Protect Your Project Scope</h1>'
        '<p class="sg-hero-sub">ScopeGuard AI instantly tells you whether a client request is covered by your contract — and generates professional Change Orders in seconds.</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    st.markdown(
        '<div class="sg-cards">'
        '<div class="sg-card"><div class="sg-card-icon">🔍</div>'
        '<div class="sg-card-title">AI Contract Analysis</div>'
        '<div class="sg-card-desc">Instant SOW vs request comparison powered by Gemini</div></div>'
        '<div class="sg-card"><div class="sg-card-icon">📊</div>'
        '<div class="sg-card-title">Task Estimator</div>'
        '<div class="sg-card-desc">Per-department hours & costs with live totals</div></div>'
        '<div class="sg-card"><div class="sg-card-icon">📄</div>'
        '<div class="sg-card-title">Change Order PDF</div>'
        '<div class="sg-card-desc">Professional, client-ready documents in one click</div></div>'
        '<div class="sg-card"><div class="sg-card-icon">🛡️</div>'
        '<div class="sg-card-title">Anti-Hallucination</div>'
        '<div class="sg-card-desc">Fuzzy-match guardrail verifies every AI citation</div></div>'
        '</div>',
        unsafe_allow_html=True,
    )
    
    st.markdown(
        '<div style="max-width:850px; margin: 0 auto; background:#ffffff; border:1px solid #bbf7d0; '
        'border-radius:16px; padding:24px 32px; box-shadow: 0 4px 20px rgba(22,163,74,.05); text-align:center;">'
        '<h3 style="color:#14532d; font-size:1.1rem; margin-bottom:16px;">How It Works</h3>'
        '<div style="display:flex; justify-content:space-between; gap:16px; text-align:left;">'
        '<div style="flex:1;"><strong style="color:#16a34a;">1. Upload SOW</strong><br><span style="font-size:.8rem; color:#4b7a5e;">Upload your signed PDF contract.</span></div>'
        '<div style="flex:1;"><strong style="color:#16a34a;">2. Paste Request</strong><br><span style="font-size:.8rem; color:#4b7a5e;">Client asks for a new feature? Paste it in the chat.</span></div>'
        '<div style="flex:1;"><strong style="color:#16a34a;">3. Get Estimate</strong><br><span style="font-size:.8rem; color:#4b7a5e;">AI flags out-of-scope work & estimates hours.</span></div>'
        '<div style="flex:1;"><strong style="color:#16a34a;">4. Export PDF</strong><br><span style="font-size:.8rem; color:#4b7a5e;">Download a ready-to-sign Change Order.</span></div>'
        '</div></div>',
        unsafe_allow_html=True
    )
    
    st.markdown(
        '<div class="sg-pricing-section">'
        '<h2 class="sg-pricing-title">Choose Your Protection Plan</h2>'
        '<div class="sg-pricing-grid">'
        
        '<!-- Free Plan -->'
        '<div class="sg-pricing-card">'
        '<div class="sg-price-name">Starter</div>'
        '<div class="sg-price-amt">Free</div>'
        '<div class="sg-price-mo">Forever</div>'
        '<div class="sg-price-desc">Perfect for freelancers and small teams getting started.</div>'
        '<div class="sg-feature"><span>✓</span> 10 SOW analyses / month</div>'
        '<div class="sg-feature"><span>✓</span> Basic change order PDFs</div>'
        '<div class="sg-feature"><span>✓</span> Standard Gemini 3.5 model</div>'
        '</div>'
        
        '<!-- Pro Plan -->'
        '<div class="sg-pricing-card pro">'
        '<div class="sg-pricing-badge">Most Popular</div>'
        '<div class="sg-price-name">Pro</div>'
        '<div class="sg-price-amt">$35</div>'
        '<div class="sg-price-mo">per user / month</div>'
        '<div class="sg-price-desc">Essential for growing agencies to eliminate scope creep entirely.</div>'
        '<div class="sg-feature"><span>✓</span> Unlimited SOW analyses</div>'
        '<div class="sg-feature"><span>✓</span> Advanced Task Estimator</div>'
        '<div class="sg-feature"><span>✓</span> Multi-department rates</div>'
        '<div class="sg-feature"><span>✓</span> Custom branded PDFs</div>'
        '</div>'
        
        '<!-- Business Plan -->'
        '<div class="sg-pricing-card">'
        '<div class="sg-price-name">Business</div>'
        '<div class="sg-price-amt">$70</div>'
        '<div class="sg-price-mo">per user / month</div>'
        '<div class="sg-price-desc">For enterprise teams needing deep integrations and high limits.</div>'
        '<div class="sg-feature"><span>✓</span> Everything in Pro</div>'
        '<div class="sg-feature"><span>✓</span> SOW bulk processing</div>'
        '<div class="sg-feature"><span>✓</span> Jira/Asana integrations</div>'
        '<div class="sg-feature"><span>✓</span> Dedicated account manager</div>'
        '</div>'
        
        '</div></div><br>',
        unsafe_allow_html=True
    )
    
    _, col, _ = st.columns([1, 1.2, 1])
    with col:
        if st.session_state.sow_filename:
            if st.button("🔄 Resume Current Project", type="primary", use_container_width=True):
                st.session_state.app_stage = 2 if st.session_state.messages else 1
                st.rerun()
        else:
            if st.button("🚀 Start New Project", type="primary", use_container_width=True, key="start_project_btn"):
                st.session_state.app_stage = 1
                st.rerun()


# ── UPLOAD SOW ───────────────────────────────────────────────────────────────
elif _stage == 1:
    st.markdown("<br><br>", unsafe_allow_html=True)
    _, col, _ = st.columns([1, 1.5, 1])
    with col:
        st.markdown(
            '<div class="sg-upload-card" style="padding: 60px 24px; border: 2px dashed #bbf7d0; background: linear-gradient(180deg, #fff, #f8fffe);">'
            '<div style="font-size:3.5rem;margin-bottom:16px;">📄</div>'
            '<div class="sg-upload-title" style="font-size:1.5rem; color:#14532d; font-weight:800;">Upload Your SOW</div>'
            '<div class="sg-upload-hint" style="font-size:1rem; color:#4b7a5e; margin: 12px 0 24px; max-width:400px; margin-left:auto; margin-right:auto;">'
            'Drop your signed PDF contract here to establish the baseline context for AI analysis.'
            '</div>'
            '</div>',
            unsafe_allow_html=True,
        )

        _uploaded = st.file_uploader(
            label="Drop your SOW PDF here",
            type=["pdf"],
            help="Text-based PDF only. Max recommended size: 10 MB.",
            key="sow_uploader",
            label_visibility="collapsed",
        )

        if _uploaded is not None:
            st.markdown(
                f'<div style="background:#052e16;border:1px solid #16a34a;border-radius:10px;'
                f'padding:10px 16px;margin:12px 0;font-size:.85rem;color:#86efac;">'
                f'✅ <strong>{_uploaded.name}</strong> ready to process</div>',
                unsafe_allow_html=True
            )
            if st.button("⚙️ Process Document", type="primary", use_container_width=True):
                with st.spinner("Extracting SOW text..."):
                    try:
                        _sow_text = extract_pdf_text(_uploaded.read())
                        if not _sow_text.strip():
                            st.error("❌ No text found. Please use a text-based PDF (not a scanned image).")
                            st.stop()
                        st.session_state.sow_text = _sow_text
                        st.session_state.sow_filename = _uploaded.name
                        st.session_state.messages = [{
                            "role": "assistant",
                            "content": (
                                "✅ Your SOW has been successfully uploaded and processed.\n\n"
                                "I'm ready to help you review client change requests against the original project agreement.\n\n"
                                "**What would you like to change or add to your project?**"
                            )
                        }]
                        st.session_state.app_stage = 2
                        st.rerun()
                    except Exception as _e:
                        st.error(f"❌ PDF extraction failed: {_e}")


# ── CHAT WORKSPACE ───────────────────────────────────────────────────────────
elif _stage == 2:

    # 1. Render conversation history
    for msg_idx, msg in enumerate(st.session_state.messages):
        avatar = "🛡️" if msg["role"] == "assistant" else "👤"
        with st.chat_message(msg["role"], avatar=avatar):
            if msg.get("content"):
                st.markdown(msg["content"])

            res = msg.get("analysis_result")
            if res:
                _status = res["status"]
                _quote = res.get("exact_verbatim_quote", "")
                _fg, _bg, _fg_light = STATUS_STYLES.get(_status, ("#90caf9", "#0d2050", "#bae6fd"))

                if not msg.get("guardrail_passed", False):
                    st.error("⚠️ **VERIFICATION ERROR:** Anti-hallucination guardrail flagged this response.")
                    st.caption(f"Fuzzy match score: **{msg.get('fuzzy_score', 0)}/100** (threshold: 85)")
                else:
                    # Status badge
                    st.markdown(
                        f'<div class="sg-status-badge" style="background:{_bg};border-color:{_fg};color:{_fg};">'
                        f'{_status}</div>',
                        unsafe_allow_html=True
                    )

                    # Analysis card
                    st.markdown(
                        f'<div class="sg-analysis-card">'
                        f'<div class="sg-analysis-label">📝 AI Reasoning</div>'
                        f'<div class="sg-analysis-text">{res["reasoning"]}</div>',
                        unsafe_allow_html=True
                    )
                    if _quote:
                        st.markdown(
                            f'<div class="sg-quote-box">📌 "{_quote}"</div>',
                            unsafe_allow_html=True
                        )
                    elif _status == "⚪ UNADDRESSED IN SOW":
                        st.markdown(
                            '<div style="margin-top:8px;padding:8px 12px;background:#1c1917;'
                            'border-left:3px solid #78716c;border-radius:0 6px 6px 0;'
                            'font-size:.82rem;color:#a8a29e;">No specific clause found in the SOW.</div>',
                            unsafe_allow_html=True
                        )
                    st.markdown('</div>', unsafe_allow_html=True)

                    # Task estimation
                    tasks = msg.get("tasks", [])
                    if _status == "🟢 IN-SCOPE":
                        st.markdown(
                            '<div class="sg-inscope-banner">'
                            '<div class="sg-inscope-icon">✅</div>'
                            '<div class="sg-inscope-text"><strong>Covered under SOW</strong> — '
                            'This request is already included in your signed contract. '
                            'No additional fee or Change Order is required.</div>'
                            '</div>',
                            unsafe_allow_html=True
                        )
                    elif tasks:
                        if _status == "🟡 PARTIALLY IN-SCOPE":
                            st.markdown(
                                '<div style="background:#1c0f00;border:1px solid #78350f;border-radius:10px;'
                                'padding:10px 16px;margin:8px 0;font-size:.82rem;color:#fde68a;">'
                                'ℹ️ Only the additional/out-of-scope portion needs to be estimated below.</div>',
                                unsafe_allow_html=True
                            )

                        with st.expander("🔧 Task Estimator", expanded=not bool(msg.get("pdf_bytes"))):
                            et_key = f"editable_tasks_{msg_idx}"
                            if et_key not in st.session_state:
                                st.session_state[et_key] = [
                                    {
                                        "name": t["task"],
                                        "department": t.get("department", "Backend Development"),
                                        "hours": 0.0,
                                        "days": 0.0,
                                        "denied": False,
                                    }
                                    for t in tasks
                                ]
                            # Ensure old entries have new fields
                            for _t in st.session_state[et_key]:
                                _t.setdefault("days", 0.0)
                                _t.setdefault("denied", False)

                            # Column headers
                            st.markdown(
                                '<div class="sg-task-header" style="grid-template-columns: 3.5fr 2.5fr 1fr 1fr 1.5fr 0.8fr; gap: 1rem;">'
                                '<span>Task</span>'
                                '<span>Department</span>'
                                '<span>Hours</span>'
                                '<span>Days</span>'
                                '<span style="text-align:right;">Cost</span>'
                                '<span style="text-align:center;">Status</span>'
                                '</div>',
                                unsafe_allow_html=True
                            )

                            _total_cost = 0.0
                            _total_hours = 0.0
                            _task_data_for_pdf = []
                            _tasks_to_delete = []

                            def _cb_hrs(m, t_idx):
                                val = st.session_state.get(f"hrs_{m}_{t_idx}", "0")
                                try: h = max(0.0, float(val) if val.strip() else 0.0)
                                except ValueError: h = 0.0
                                st.session_state[f"editable_tasks_{m}"][t_idx]["hours"] = h
                                st.session_state[f"editable_tasks_{m}"][t_idx]["days"] = round(h / 8, 2)
                                d = st.session_state[f"editable_tasks_{m}"][t_idx]["days"]
                                st.session_state[f"days_{m}_{t_idx}"] = str(int(d)) if d == int(d) else f"{d:.1f}"

                            def _cb_days(m, t_idx):
                                val = st.session_state.get(f"days_{m}_{t_idx}", "0")
                                try: d = max(0.0, float(val) if val.strip() else 0.0)
                                except ValueError: d = 0.0
                                st.session_state[f"editable_tasks_{m}"][t_idx]["days"] = d
                                st.session_state[f"editable_tasks_{m}"][t_idx]["hours"] = round(d * 8, 1)
                                h = st.session_state[f"editable_tasks_{m}"][t_idx]["hours"]
                                st.session_state[f"hrs_{m}_{t_idx}"] = str(int(h)) if h == int(h) else f"{h:.1f}"

                            for t_idx, t in enumerate(st.session_state[et_key]):
                                dk  = f"dept_{msg_idx}_{t_idx}"
                                hk  = f"hrs_{msg_idx}_{t_idx}"
                                dyk = f"days_{msg_idx}_{t_idx}"
                                nk  = f"name_{msg_idx}_{t_idx}"
                                deny_key = f"deny_{msg_idx}_{t_idx}"

                                is_denied = t.get("denied", False)

                                if is_denied:
                                    # Show struck-through denied row
                                    st.markdown(
                                        f'<div style="display:flex;align-items:center;gap:10px;'
                                        f'padding:8px 12px;background:#fff5f5;border-radius:8px;'
                                        f'border:1px solid #fca5a5;margin:4px 0;opacity:.7;">'
                                        f'<span style="flex:1;text-decoration:line-through;color:#9ca3af;font-size:.85rem;">{t["name"]}</span>'
                                        f'<span style="font-size:.75rem;color:#ef4444;font-weight:700;">❌ DENIED</span>'
                                        f'</div>',
                                        unsafe_allow_html=True
                                    )
                                    undo_key = f"undo_{msg_idx}_{t_idx}"
                                    if st.button("↩ Undo", key=undo_key):
                                        st.session_state[et_key][t_idx]["denied"] = False
                                        st.rerun()
                                    continue

                                c1, c2, c3, c4, c5, c6 = st.columns([3.5, 2.5, 1, 1, 1.5, 0.8])
                                with c1:
                                    t["name"] = st.text_input("Task", value=t["name"], key=nk, label_visibility="collapsed")
                                with c2:
                                    cur_dept = t["department"] if t["department"] in DEPARTMENTS else DEPARTMENTS[0]
                                    t["department"] = st.selectbox("Dept", DEPARTMENTS, index=DEPARTMENTS.index(cur_dept), key=dk, label_visibility="collapsed")
                                with c3:
                                    st.text_input("Hrs", value=str(int(t["hours"])) if t["hours"] == int(t["hours"]) else f"{t['hours']:.1f}", 
                                                  key=hk, label_visibility="collapsed", placeholder="0", 
                                                  on_change=_cb_hrs, args=(msg_idx, t_idx))
                                with c4:
                                    st.text_input("Days", value=str(int(t["days"])) if t["days"] == int(t["days"]) else f"{t['days']:.1f}", 
                                                  key=dyk, label_visibility="collapsed", placeholder="0", 
                                                  on_change=_cb_days, args=(msg_idx, t_idx))

                                with c5:
                                    _rate = get_dept_rate(t["department"])
                                    _line = t["hours"] * _rate
                                    if _line > 0:
                                        st.markdown(f'<div style="padding-top:8px;"><span class="sg-cost-pill">${_line:,.0f}</span></div>', unsafe_allow_html=True)
                                    else:
                                        st.markdown(f'<div style="padding-top:12px;font-size:.85rem;color:#475569;">—</div>', unsafe_allow_html=True)
                                with c6:
                                    if st.button("❌", key=deny_key, help="Deny this task — client rejected it"):
                                        st.session_state[et_key][t_idx]["denied"] = True
                                        st.rerun()

                                _total_hours += t["hours"]
                                _total_cost += _line
                                _task_data_for_pdf.append({
                                    "task": t["name"], "department": t["department"],
                                    "hours": t["hours"], "rate": _rate, "line_cost": _line
                                })

                            # Add task button
                            if st.button("➕ Add Custom Task", key=f"add_task_{msg_idx}"):
                                st.session_state[et_key].append({"name": "New Task", "department": DEPARTMENTS[0], "hours": 0.0, "days": 0.0, "denied": False})
                                st.rerun()

                            # Totals
                            _n_denied = sum(1 for _t in st.session_state[et_key] if _t.get("denied"))
                            _denied_note = f' &nbsp;·&nbsp; <span style="color:#ef4444;font-size:.75rem;">{_n_denied} task(s) denied</span>' if _n_denied else ""
                            st.markdown(
                                f'<div class="sg-totals-row">'
                                f'<div class="sg-totals-hrs">⏱ {_total_hours:.1f} hrs total{_denied_note}</div>'
                                f'<div class="sg-totals-cost">${_total_cost:,.2f}</div>'
                                f'</div>',
                                unsafe_allow_html=True
                            )
                            st.markdown("<br>", unsafe_allow_html=True)

                            # PDF actions
                            if msg.get("pdf_bytes"):
                                st.success("✅ Your Change Order is ready.")
                                st.download_button(
                                    label="⬇️ Download Change Order PDF",
                                    data=msg["pdf_bytes"],
                                    file_name="ScopeGuard_Change_Order.pdf",
                                    mime="application/pdf",
                                    key=f"dl_pdf_{msg_idx}",
                                    type="primary",
                                    use_container_width=True,
                                )
                            else:
                                if _total_hours > 0:
                                    if st.button("📄 Generate Change Order PDF", key=f"gen_pdf_{msg_idx}", type="primary", use_container_width=True):
                                        with st.spinner("Generating PDF..."):
                                            pdf_b = generate_change_order_pdf(
                                                task_data=_task_data_for_pdf,
                                                total_cost=_total_cost,
                                                client_request=msg.get("user_request", "Client Request"),
                                            )
                                            st.session_state.messages[msg_idx]["pdf_bytes"] = pdf_b
                                            st.rerun()
                                else:
                                    st.markdown(
                                        '<div style="text-align:center;padding:12px;'
                                        'font-size:.82rem;color:#475569;">'
                                        'ℹ️ Enter estimated hours or days to generate a Change Order</div>',
                                        unsafe_allow_html=True
                                    )

    if len(st.session_state.messages) == 1:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.markdown('<div style="text-align:center; color:#4b7a5e; font-size:.9rem; font-weight:600; margin-bottom:16px;">💡 Try asking about...</div>', unsafe_allow_html=True)
        st.markdown(
            '<div style="display:flex; gap:12px; justify-content:center; flex-wrap:wrap;">'
            '<div style="background:#f0fdf4; border:1px solid #bbf7d0; padding:12px 20px; border-radius:100px; font-size:.85rem; color:#14532d; box-shadow:0 2px 8px rgba(22,163,74,0.1);">"Add a dark mode toggle"</div>'
            '<div style="background:#f0fdf4; border:1px solid #bbf7d0; padding:12px 20px; border-radius:100px; font-size:.85rem; color:#14532d; box-shadow:0 2px 8px rgba(22,163,74,0.1);">"Integrate Apple Pay on checkout"</div>'
            '<div style="background:#f0fdf4; border:1px solid #bbf7d0; padding:12px 20px; border-radius:100px; font-size:.85rem; color:#14532d; box-shadow:0 2px 8px rgba(22,163,74,0.1);">"Change the logo colors"</div>'
            '</div><br><br><br>',
            unsafe_allow_html=True
        )

    # 2. Chat input
    if prompt := st.chat_input("Describe the client's change request..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.rerun()

    # 3. Process latest user message
    if st.session_state.messages and st.session_state.messages[-1]["role"] == "user":
        prompt = st.session_state.messages[-1]["content"]

        with st.chat_message("assistant", avatar="🛡️"):
            if not is_valid_change_request(prompt):
                err_msg = "Please enter a valid client change request (e.g., 'add a dark mode toggle')."
                st.markdown(err_msg)
                st.session_state.messages.append({"role": "assistant", "content": err_msg})
            else:
                with st.spinner("Analyzing against SOW..."):
                    try:
                        raw = analyze_with_gemini(st.session_state.sow_text, prompt)
                        parsed = clean_json(raw)
                    except Exception as _e:
                        err_msg = f"❌ API Error: {str(_e)}"
                        st.error(err_msg)
                        st.session_state.messages.append({"role": "assistant", "content": err_msg})
                        st.stop()

                    if not parsed or (REQUIRED_KEYS - set(parsed.keys())) or parsed.get("status") not in VALID_STATUSES:
                        err_msg = "❌ AI returned an invalid response format. Please try again."
                        st.error(err_msg)
                        st.session_state.messages.append({"role": "assistant", "content": err_msg})
                        st.stop()

                    _quote = parsed.get("exact_verbatim_quote", "")
                    _status = parsed["status"]
                    _quote_words = _quote.strip().split() if _quote.strip() else []
                    _needs_verify = _status != "⚪ UNADDRESSED IN SOW" and len(_quote_words) > 5

                    _guardrail_ok = True
                    _score = None
                    if _needs_verify:
                        _score = verify_quote(_quote, st.session_state.sow_text)
                        if _score < 85:
                            _guardrail_ok = False

                    _raw_t = parsed.get("technical_tasks_list", [])
                    _tasks = []
                    for _t in _raw_t:
                        if isinstance(_t, dict):
                            _tasks.append({"task": str(_t.get("task", _t)), "department": _t.get("department", "Backend Development")})
                        else:
                            _tasks.append({"task": str(_t), "department": "Backend Development"})

                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": "",
                        "analysis_result": parsed,
                        "fuzzy_score": _score,
                        "guardrail_passed": _guardrail_ok,
                        "tasks": _tasks,
                        "user_request": prompt,
                    })
                    st.rerun()

# =============================================================================
# AUTO-SAVE ON EVERY RUN
# =============================================================================
_save_session()
