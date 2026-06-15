"""
Phi Beta Alpha — Lineage Check Web Application (multi-state)
"""

import io
import os
import time
import json
import sqlite3
import hashlib
import datetime
import functools
import base64
from pathlib import Path
from flask import (Flask, render_template, request, jsonify, redirect,
                   session, abort, send_file, url_for)

from search import (
    run_search, open_db, TARGET_STATES, get_states_with_ocr,
    GA_COUNTY_NHGIS, get_family_cluster,
)

# Lazy-loaded; keeps startup fast and backward_walk optional
_backward_walk_mod = None

def _bw():
    global _backward_walk_mod
    if _backward_walk_mod is None:
        import sys as _sys
        _scripts = str(Path(__file__).parent.parent / "scripts")
        if _scripts not in _sys.path:
            _sys.path.insert(0, _scripts)
        import backward_walk as _m
        _backward_walk_mod = _m
    return _backward_walk_mod

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24).hex())
DB_PATH = Path.home() / "Documents/phi-beta-alpha/processed/lineage_1870.db"
CACHE_DIR = Path.home() / "Documents/phi-beta-alpha/output/multi_state_reels"

# Admin auth — set ADMIN_PASSWORD env var (no default; unset = admin disabled)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


def _require_admin(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not ADMIN_PASSWORD:
            return f(*args, **kwargs)  # dev mode: no password set = open
        if not session.get("admin_ok"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def _make_qr_data_uri(url: str) -> str:
    """Generate a QR code PNG as a base64 data URI."""
    try:
        import qrcode, qrcode.image.pil
        from PIL import Image
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=6, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#1a3a8f", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""

GA_COUNTIES = sorted(c.title() for c in GA_COUNTY_NHGIS.keys())

# NARA M593 reel catalog for status page
REEL_CATALOG = {
    "Alabama":        [(8, "Dallas County (Selma)", 1), (15, "Limestone-Lowndes", 1), (16, "Macon County", 1)],
    "Florida":        [(121, "Gadsden-Jefferson", 1), (122, "Lafayette-Leon", 1)],
    "South Carolina": [(1135, "Beaufort (Sea Islands)", 1), (1136, "Charleston", 1),
                       (1139, "Colleton County", 1), (1147, "Hampton-Horry (Badger area)", 1)],
    "North Carolina": [(928, "Edgecombe", 1), (929, "Franklin-Granville", 1), (934, "Halifax", 1)],
    "Mississippi":    [(638, "Adams/Natchez", 1), (640, "Bolivar-Claiborne", 1)],
    "Kentucky":       [(408, "Jefferson/Louisville", 1)],
    "Louisiana":      [(471, "New Orleans", 1), (462, "Iberia-Iberville", 1), (479, "St. Mary Parish", 1)],
    "Virginia":       [(1285, "Mecklenburg", 1), (1280, "Lunenburg-Mecklenburg", 1)],
    "New York":       [(870, "Manhattan Pt 1", 1), (871, "Manhattan Pt 2", 1), (872, "Manhattan Pt 3", 1),
                       (860, "Kings County (Brooklyn)", 1)],
}

# Known enslaver database
ENSLAVER_DB = {
    "bacon": {
        "name": "Edwin Henry Bacon, Sr.",
        "birth_year": 1811,
        "death_year": 1877,
        "county": "Liberty",
        "state": "Georgia",
        "enslaved_count": 93,
        "source_year": 1850,
        "notes": (
            "Lawyer, planter, Liberty County GA state legislator. "
            "Captain/Major of Liberty Independent Troops (CSA). "
            "Buried: Bonaventure Cemetery, Savannah, GA."
        ),
        "evidence": (
            "Cox 1850 Georgia Slave Schedule: 93 enslaved, Liberty County. "
            "No Bacon slaveholders in McIntosh County (1850 or 1860). "
            "62 Bacon-surnamed freedpeople cluster in Liberty County 1870 "
            "(NARA M593 Reel 162). "
            "Harriet Bacon labor contract at Harris Neck, McIntosh County, 1866 "
            "(Freedmen's Bureau NARA M798, Roll 26)."
        ),
        "probable": True,
    },
    "badger": {
        "name": "Unknown (Hampton County SC enslaver)",
        "birth_year": None,
        "death_year": None,
        "county": "Hampton",
        "state": "South Carolina",
        "enslaved_count": None,
        "source_year": 1850,
        "notes": (
            "No prominent Badger slaveholders identified in Hampton County or SC Lowcountry. "
            "The Badger surname was adopted after emancipation. "
            "Andrew Badger (b.1870) and Richard Badger (b.1871) both in Lawton Township, Hampton County SC — "
            "likely born enslaved in the same household near Lawton, SC."
        ),
        "evidence": (
            "1900 Hampton County SC census: Andrew Badger (b.1870, Farmer) and Richard Badger (b.1871) "
            "confirmed in Lawton Township, ED 58. "
            "No Badger-surnamed slaveholders found in Hampton County 1850/1860 slave schedules. "
            "Probable enslaver surname not yet identified — SC death certificate for JHB would confirm."
        ),
        "probable": False,
    },
    "freeman": {
        "name": "Various Freeman slaveholders (GA)",
        "birth_year": None,
        "death_year": None,
        "county": "Multiple",
        "state": "Georgia",
        "enslaved_count": None,
        "source_year": 1860,
        "notes": "Common name adopted by freedpeople after emancipation.",
        "evidence": "Freedmen's Bureau labor contracts, Liberty and McIntosh Counties, GA.",
        "probable": True,
    },
}


# ── Pipeline activity cache ────────────────────────────────────────────────────
_pipeline_cache = {"data": None, "ts": 0}


def get_pipeline_active():
    """Return pipeline status if a reel was processed within the last 90 seconds."""
    now = time.time()
    if now - _pipeline_cache["ts"] < 15:
        return _pipeline_cache["data"]
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=3)
        row = conn.execute("""
            SELECT reel_number, state,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                   COUNT(*) as total,
                   SUM(records_saved) as records,
                   MAX(processed_at) as last_at
            FROM pipeline_progress
            GROUP BY reel_number, state
            ORDER BY last_at DESC
            LIMIT 1
        """).fetchone()
        conn.close()
        if row and row[5]:
            last = datetime.datetime.fromisoformat(row[5])
            age = (datetime.datetime.now() - last).total_seconds()
            if age < 90:
                data = {
                    "reel": row[0],
                    "state": row[1] or "Unknown",
                    "pages_done": int(row[2] or 0),
                    "pages_total": int(row[3] or 0),
                    "records": int(row[4] or 0),
                    "pct": round((row[2] or 0) / max(row[3] or 1, 1) * 100),
                }
                _pipeline_cache["data"] = data
                _pipeline_cache["ts"] = now
                return data
    except Exception:
        pass
    _pipeline_cache["data"] = None
    _pipeline_cache["ts"] = now
    return None


@app.context_processor
def inject_globals():
    return {"pipeline_active": get_pipeline_active()}


# ── Stats ──────────────────────────────────────────────────────────────────────
def get_stats():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        ocr_total    = conn.execute("SELECT COUNT(*) FROM ocr_records").fetchone()[0]
        ocr_black    = conn.execute("SELECT COUNT(*) FROM ocr_records WHERE race_code=2").fetchone()[0]
        pipe_total   = conn.execute("SELECT COUNT(*) FROM census_ocr_georgia_1870").fetchone()[0]
        pipe_black   = conn.execute("SELECT COUNT(*) FROM census_ocr_georgia_1870 WHERE is_black=1").fetchone()[0]
        multi_total  = conn.execute("SELECT COUNT(*) FROM census_ocr_1870").fetchone()[0]
        multi_black  = conn.execute("SELECT COUNT(*) FROM census_ocr_1870 WHERE is_black=1").fetchone()[0]
        ipums_total  = conn.execute("SELECT COUNT(*) FROM census WHERE YEAR=1870").fetchone()[0]
        bureau_total = conn.execute("SELECT COUNT(*) FROM bureau_patients").fetchone()[0]
        states_covered = get_states_with_ocr(conn)
        conn.close()
        total_named = ocr_total + pipe_total + multi_total
        total_black = ocr_black + pipe_black + multi_black
        return {
            "ocr_records":    ocr_total,
            "ocr_black":      ocr_black,
            "pipe_records":   pipe_total,
            "pipe_black":     pipe_black,
            "multi_records":  multi_total,
            "multi_black":    multi_black,
            "ipums_total":    ipums_total,
            "bureau_total":   bureau_total,
            "total_named":    total_named,
            "total_black":    total_black,
            "states_covered": sorted(states_covered),
            "state_count":    len(states_covered),
        }
    except Exception:
        return {}


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    stats = get_stats()
    return render_template("index.html",
                           nav_page="search",
                           counties=GA_COUNTIES,
                           states=TARGET_STATES,
                           stats=stats)


@app.route("/search", methods=["POST"])
def search():
    last_name  = request.form.get("last_name", "").strip()
    first_name = request.form.get("first_name", "").strip()
    birth_year = request.form.get("birth_year", "").strip()
    state      = request.form.get("state", "Georgia").strip() or "Georgia"
    county     = request.form.get("county", "").strip()
    window     = int(request.form.get("window", 15))

    if not last_name:
        stats = get_stats()
        return render_template("index.html", nav_page="search",
                               counties=GA_COUNTIES, states=TARGET_STATES,
                               stats=stats, error="Last name is required.")

    birth_year_int = None
    if birth_year:
        try:
            birth_year_int = int(birth_year)
        except ValueError:
            stats = get_stats()
            return render_template("index.html", nav_page="search",
                                   counties=GA_COUNTIES, states=TARGET_STATES,
                                   stats=stats, error="Birth year must be a number.")

    state_for_search = None if state == "All States" else state
    results, no_ocr_stub = run_search(
        last_name=last_name,
        first_name=first_name,
        birth_year=birth_year_int,
        state=state_for_search,
        county=county or None,
        window=window,
    )

    # Family cluster — other Black family members with same surname
    family_cluster = []
    if results:
        conn = open_db()
        exclude_ids = {r["match"]["ocr_id"] for r in results}
        family_cluster = get_family_cluster(conn, last_name, state_for_search, exclude_ids)
        conn.close()

    return render_template(
        "results.html",
        nav_page="search",
        results=results,
        no_ocr_stub=no_ocr_stub,
        family_cluster=family_cluster,
        query={
            "last_name":  last_name,
            "first_name": first_name,
            "birth_year": birth_year,
            "state":      state,
            "county":     county,
        },
        counties=GA_COUNTIES,
        states=TARGET_STATES,
        stats=get_stats(),
    )


@app.route("/lineage/<member_id>")
def lineage_view(member_id):
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    member = conn.execute("SELECT * FROM members WHERE member_id = ?", (member_id,)).fetchone()
    if not member:
        conn.close()
        return "Member not found", 404
    ancestors = conn.execute(
        "SELECT * FROM lineage_ancestors WHERE member_id = ? ORDER BY generation",
        (member_id,)
    ).fetchall()
    conn.close()
    return render_template("lineage.html", nav_page="members",
                           member=dict(member),
                           ancestors=[dict(a) for a in ancestors])


@app.route("/certificate/<member_id>")
def certificate(member_id):
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    member = conn.execute("SELECT * FROM members WHERE member_id = ?", (member_id,)).fetchone()
    if not member:
        conn.close()
        return "Member not found", 404
    ancestors = conn.execute(
        "SELECT * FROM lineage_ancestors WHERE member_id = ? ORDER BY generation",
        (member_id,)
    ).fetchall()
    ancestors = [dict(a) for a in ancestors]
    conn.close()

    surname_key = (dict(member).get("last_name") or "").lower()
    enslaver = ENSLAVER_DB.get(surname_key)

    evidence_records = []
    for anc in ancestors:
        if anc.get("ipums_histid"):
            evidence_records.append({
                "source":   "IPUMS Linked Census Sample",
                "year":     "1870",
                "person":   f"{anc['first_name']} {anc['last_name']}",
                "location": f"{anc.get('birth_county','?')} County, {anc.get('birth_state','GA')}",
                "citation": f"HISTID: {anc['ipums_histid'][:20]}…",
            })
        if anc.get("fs_household_id"):
            evidence_records.append({
                "source":   "FamilySearch Historical Records",
                "year":     "1880+",
                "person":   f"{anc['first_name']} {anc['last_name']}",
                "location": f"{anc.get('birth_county','?')} County, {anc.get('birth_state','GA')}",
                "citation": f"ARK: {anc['fs_household_id']}",
            })

    if surname_key == "badger":
        evidence_records.append({
            "source":   "1930 U.S. Federal Census",
            "year":     "1930",
            "person":   "John House Badger (age 38) + family",
            "location": "Jones Township, McIntosh County, Georgia",
            "citation": "NARA; FamilySearch ark:/61903/3:1:33SQ-GRHS-W2K",
        })
        evidence_records.append({
            "source":   "1900 U.S. Federal Census",
            "year":     "1900",
            "person":   "Andrew Badger (age 30) + 9 children",
            "location": "Lawton Township, Hampton County, South Carolina",
            "citation": "NARA Reel 1531 (12thcensusofpopu1531unit), page 268, ED 58",
        })
        evidence_records.append({
            "source":   "1900 U.S. Federal Census (Soundex only)",
            "year":     "1900",
            "person":   "Richard Badger (age 29) + wife Lana + 3 children",
            "location": "Lawton Township, Hampton County, South Carolina",
            "citation": "NARA Soundex card, Reel 1531, ED 58, Sheet 21, Line 64",
        })

    if surname_key == "bacon":
        evidence_records.append({
            "source":   "Freedmen's Bureau — Darien Dispensary",
            "year":     "1865–1868",
            "person":   "Samuel Bacon (age 71), Hannah Bacon, Benjamin Bacon + 8 others",
            "location": "McIntosh County, Georgia",
            "citation": "NARA M1903, Roll 61 — 11 Bacon-surnamed patients confirmed",
        })
        evidence_records.append({
            "source":   "Freedmen's Bureau — Labor Contracts",
            "year":     "1866–1867",
            "person":   "11 Bacon-surnamed freedpeople (Jack, Caroline, Stephen, Joseph, etc.)",
            "location": "Liberty County, Georgia",
            "citation": "NARA M798, Roll 26 — contracts with Winn, Way, Hines, Quarterman plantations",
        })
        evidence_records.append({
            "source":   "Edwin H. Bacon Estate",
            "year":     "1877",
            "person":   "Edwin Henry Bacon Sr. (enslaver)",
            "location": "Liberty County, Georgia (buried Bonaventure Cemetery, Savannah)",
            "citation": "Chatham County death records; Liberty County deed books 1844–1868",
        })

    # Use stable cert_id from DB, falling back to MD5 for legacy members
    member_dict = dict(member)
    cert_id = member_dict.get("cert_id") or hashlib.md5(f"{member_id}-{datetime.date.today()}".encode()).hexdigest()[:12].upper()
    today = datetime.date.today().strftime("%B %d, %Y")

    verify_url = request.host_url.rstrip("/") + f"/verify/{cert_id}"
    qr_data_uri = _make_qr_data_uri(verify_url)

    return render_template("certificate.html",
                           member=member_dict, ancestors=ancestors,
                           enslaver=enslaver, evidence_records=evidence_records,
                           cert_id=cert_id, today=today,
                           verify_url=verify_url, qr_data_uri=qr_data_uri)


@app.route("/members")
def members_view():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        member_rows = conn.execute("""
            SELECT m.member_id, m.first_name, m.last_name, m.birth_year,
                   m.birth_state, m.birth_county, m.notes,
                   COUNT(a.ancestor_id) as ancestor_count,
                   SUM(a.verified) as verified_count
            FROM members m
            LEFT JOIN lineage_ancestors a ON a.member_id = m.member_id
            GROUP BY m.member_id
            ORDER BY m.last_name, m.first_name
        """).fetchall()

        members = []
        for m in member_rows:
            m = dict(m)
            ancestors = conn.execute("""
                SELECT first_name, last_name, birth_year, birth_county,
                       birth_state, relationship, ipums_tier, verified, notes
                FROM lineage_ancestors
                WHERE member_id = ?
                ORDER BY generation
            """, (m["member_id"],)).fetchall()
            m["ancestors"] = [dict(a) for a in ancestors]
            # Build chain label: oldest verified ancestor → member
            verified = [a for a in m["ancestors"] if a["verified"]]
            if verified:
                oldest = verified[0]
                m["chain_root"] = f"{oldest['first_name']} {oldest['last_name']}"
                m["chain_root_year"] = oldest["birth_year"]
                m["chain_root_loc"] = f"{oldest['birth_county'] or ''}, {oldest['birth_state'] or ''}".strip(", ")
            members.append(m)

        conn.close()
        return render_template("members.html", nav_page="members", members=members)
    except Exception as e:
        return f"Error: {e}", 500


@app.route("/status")
def pipeline_status():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)

        # Per-reel pipeline progress from pipeline_progress table
        prog_rows = conn.execute("""
            SELECT reel_number, state,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                   SUM(CASE WHEN status='blank' THEN 1 ELSE 0 END) as blank,
                   COUNT(*) as total_pages,
                   SUM(records_saved) as records,
                   MAX(processed_at) as last_at
            FROM pipeline_progress
            GROUP BY reel_number, state
        """).fetchall()
        prog_by_reel = {}
        for r in prog_rows:
            prog_by_reel[r[0]] = {
                "success": r[2] or 0,
                "blank": r[3] or 0,
                "total_pages": r[4] or 0,
                "records": r[5] or 0,
                "last_at": r[6],
            }

        # State-level OCR counts
        ocr_rows = conn.execute("""
            SELECT state, COUNT(*) as cnt, SUM(is_black) as blk
            FROM census_ocr_1870 GROUP BY state
        """).fetchall()
        ocr_by_state = {r[0]: {"total": r[1], "black": r[2] or 0} for r in ocr_rows}

        ga_count = conn.execute("SELECT COUNT(*) FROM census_ocr_georgia_1870").fetchone()[0]
        ga_black = conn.execute("SELECT COUNT(*) FROM census_ocr_georgia_1870 WHERE is_black=1").fetchone()[0]

        total_multi = conn.execute("SELECT COUNT(*) FROM census_ocr_1870").fetchone()[0]
        total_multi_black = conn.execute("SELECT COUNT(*) FROM census_ocr_1870 WHERE is_black=1").fetchone()[0]

        conn.close()
    except Exception:
        prog_by_reel, ocr_by_state = {}, {}
        ga_count = ga_black = total_multi = total_multi_black = 0

    now = datetime.datetime.now()

    def reel_zip_exists(reel_num):
        p = CACHE_DIR / f"populationschedu{str(reel_num).zfill(4)}unit_jp2.zip"
        return p.exists() and p.stat().st_size > 1_000_000

    state_status = []
    for state, reels in REEL_CATALOG.items():
        ocr = ocr_by_state.get(state, {"total": 0, "black": 0})
        reel_info = []
        state_is_active = False

        for reel_num, county, priority in reels:
            prog = prog_by_reel.get(reel_num, {})
            downloaded = reel_zip_exists(reel_num)
            pages_done = prog.get("success", 0) + prog.get("blank", 0)
            pages_total = prog.get("total_pages", 0)
            records = prog.get("records", 0)
            last_at = prog.get("last_at")

            is_active = False
            if last_at:
                age = (now - datetime.datetime.fromisoformat(last_at)).total_seconds()
                is_active = age < 90

            if is_active:
                status = "active"
                state_is_active = True
            elif pages_done > 0 and pages_done >= pages_total > 0:
                status = "complete"
            elif pages_done > 0:
                status = "active" if is_active else "partial"
            elif downloaded:
                status = "downloaded"
            else:
                status = "pending"

            pct = round(pages_done / max(pages_total, 1) * 100) if pages_total else 0

            reel_info.append({
                "reel": reel_num,
                "county": county,
                "downloaded": downloaded,
                "pages_done": pages_done,
                "pages_total": pages_total,
                "records": records,
                "status": status,
                "pct": pct,
                "is_active": is_active,
            })

        state_status.append({
            "state": state,
            "total_records": ocr.get("total", 0) + sum(r["records"] for r in reel_info),
            "black_records": ocr.get("black", 0),
            "reels": reel_info,
            "has_ocr": ocr.get("total", 0) > 0,
            "is_active": state_is_active,
        })

    states_with_data = sum(1 for s in state_status if s["total_records"] > 0)

    return render_template("status.html", nav_page="status",
                           state_status=state_status,
                           ga_count=ga_count,
                           ga_black=ga_black,
                           total_multi_records=total_multi,
                           total_multi_black=total_multi_black,
                           states_with_data=states_with_data)


@app.route("/api/pipeline-progress")
def api_pipeline_progress():
    """Live pipeline progress from pipeline_progress table."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        rows = conn.execute("""
            SELECT reel_number, state,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                   COUNT(*) as total_pages,
                   SUM(records_saved) as records,
                   MAX(processed_at) as last_at
            FROM pipeline_progress
            GROUP BY reel_number, state
            ORDER BY last_at DESC
        """).fetchall()
        conn.close()
        result = []
        now = datetime.datetime.now()
        for r in rows:
            last_at = r[5]
            is_active = False
            if last_at:
                age = (now - datetime.datetime.fromisoformat(last_at)).total_seconds()
                is_active = age < 90
            result.append({
                "reel": r[0], "state": r[1],
                "success": r[2] or 0, "total_pages": r[3] or 0,
                "records": r[4] or 0, "last_at": last_at,
                "is_active": is_active,
                "pct": round((r[2] or 0) / max(r[3] or 1, 1) * 100),
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/search")
def api_search():
    last_name  = request.args.get("last", "").strip()
    first_name = request.args.get("first", "").strip()
    birth_year = request.args.get("year", "")
    state      = request.args.get("state", "Georgia").strip()
    county     = request.args.get("county", "").strip()

    if not last_name:
        return jsonify({"error": "last name required"}), 400

    birth_year_int = int(birth_year) if birth_year.isdigit() else None
    state_for_search = None if state == "All States" else state
    results, no_ocr_stub = run_search(last_name, first_name, birth_year_int,
                                      state_for_search, county or None)

    return jsonify({
        "results": [
            {
                "name":           f"{r['match']['first_name']} {r['match']['last_name']}",
                "state":          r["match"]["state"],
                "county":         r["match"]["county"],
                "birth_year":     r["match"]["birth_year"],
                "confidence":     r["confidence"],
                "tier":           r["tier"],
                "household_size": len(r["household"]),
                "ipums_histid":   r["ipums"]["histid"] if r["ipums"] else None,
                "source":         r["match"]["source_table"],
            }
            for r in results
        ],
        "demographic_context": no_ocr_stub,
    })


@app.route("/api/states")
def api_states():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        rows = conn.execute("""
            SELECT state, COUNT(*) as cnt, SUM(is_black) as black_cnt
            FROM census_ocr_1870 GROUP BY state
        """).fetchall()
        ga_cnt = conn.execute("SELECT COUNT(*) FROM census_ocr_georgia_1870").fetchone()[0]
        conn.close()
        state_data = {"Georgia": {"total": ga_cnt, "black": None}}
        for r in rows:
            state_data[r[0]] = {"total": r[1], "black": r[2]}
        return jsonify(state_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/members")
def api_members():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT m.member_id, m.first_name, m.last_name, m.birth_year,
                   m.birth_state, m.birth_county,
                   COUNT(a.ancestor_id) as ancestor_count,
                   SUM(a.verified) as verified_count
            FROM members m
            LEFT JOIN lineage_ancestors a ON a.member_id = m.member_id
            GROUP BY m.member_id
        """).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/begin", methods=["GET", "POST"])
def begin_verification():
    if request.method == "GET":
        prefill = {
            "anc_first":    request.args.get("first", ""),
            "anc_last":     request.args.get("last", ""),
            "anc_year":     request.args.get("year", ""),
            "anc_state":    request.args.get("state", "Georgia"),
            "anc_county":   request.args.get("county", ""),
            "ocr_id":       request.args.get("ocr_id", ""),
            "ipums_histid": request.args.get("histid", ""),
        }
        return render_template("begin.html", nav_page="search",
                               states=TARGET_STATES, counties=GA_COUNTIES,
                               submitted=False, prefill=prefill)

    member_name  = request.form.get("member_name", "").strip()
    member_email = request.form.get("member_email", "").strip()
    anc_first    = request.form.get("anc_first", "").strip()
    anc_last     = request.form.get("anc_last", "").strip()
    anc_year     = request.form.get("anc_year", "").strip()
    anc_state    = request.form.get("anc_state", "Georgia").strip()
    anc_county   = request.form.get("anc_county", "").strip()
    ocr_id       = request.form.get("ocr_id", "").strip()
    ipums_histid = request.form.get("ipums_histid", "").strip()
    notes        = request.form.get("notes", "").strip()

    if not all([member_name, anc_first, anc_last]):
        return render_template("begin.html", nav_page="search",
                               states=TARGET_STATES, counties=GA_COUNTIES,
                               submitted=False,
                               error="Member name and ancestor name are required.")

    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    submission_id = hashlib.md5(
        f"{member_name}-{anc_first}-{anc_last}-{datetime.datetime.now().isoformat()}".encode()
    ).hexdigest()[:12].upper()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS verification_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id TEXT UNIQUE,
            member_name TEXT,
            member_email TEXT,
            anc_first TEXT,
            anc_last TEXT,
            anc_year TEXT,
            anc_state TEXT,
            anc_county TEXT,
            ocr_id TEXT,
            ipums_histid TEXT,
            notes TEXT,
            status TEXT DEFAULT 'pending',
            submitted_at TEXT
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO verification_submissions
        (submission_id, member_name, member_email, anc_first, anc_last,
         anc_year, anc_state, anc_county, ocr_id, ipums_histid, notes, submitted_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (submission_id, member_name, member_email, anc_first, anc_last,
          anc_year, anc_state, anc_county, ocr_id, ipums_histid, notes,
          datetime.datetime.now().isoformat()))
    conn.commit()
    conn.close()

    return render_template("begin.html", nav_page="search",
                           states=TARGET_STATES, counties=GA_COUNTIES,
                           submitted=True, submission_id=submission_id,
                           anc_name=f"{anc_first} {anc_last}")


@app.route("/explore")
def explore():
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.row_factory = sqlite3.Row

    # GA pipeline counties — sorted by Black record count
    ga_counties = conn.execute("""
        SELECT county,
               COUNT(*) as total,
               SUM(is_black) as black_count
        FROM census_ocr_georgia_1870
        GROUP BY county
        HAVING black_count > 0
        ORDER BY black_count DESC
        LIMIT 40
    """).fetchall()

    # ocr_records counties (Liberty + McIntosh — fully named, most reliable)
    ocr_counties = conn.execute("""
        SELECT county,
               COUNT(*) as total,
               SUM(CASE WHEN race_code=2 THEN 1 ELSE 0 END) as black_count
        FROM ocr_records
        GROUP BY county
        ORDER BY black_count DESC
    """).fetchall()

    # Multi-state coverage
    states_data = conn.execute("""
        SELECT state,
               COUNT(*) as total,
               SUM(is_black) as black_count
        FROM census_ocr_1870
        GROUP BY state
        ORDER BY black_count DESC
    """).fetchall()

    conn.close()
    return render_template("explore.html",
                           nav_page="explore",
                           ga_counties=[dict(r) for r in ga_counties],
                           ocr_counties=[dict(r) for r in ocr_counties],
                           states_data=[dict(r) for r in states_data])


@app.route("/explore/georgia/<county>/browse")
def browse_county(county):
    page     = max(1, int(request.args.get("page", 1)))
    per_page = 60
    offset   = (page - 1) * per_page
    sort     = request.args.get("sort", "name")  # name | age | occupation

    order = {
        "name":       "last_name, first_name",
        "age":        "CAST(age AS INTEGER)",
        "occupation": "occupation, last_name",
    }.get(sort, "last_name, first_name")

    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row

    total = conn.execute(
        "SELECT COUNT(*) FROM census_ocr_georgia_1870 WHERE LOWER(county)=LOWER(?) AND is_black=1",
        (county,)
    ).fetchone()[0]

    records = conn.execute(f"""
        SELECT first_name, last_name, age, sex, occupation, birthplace,
               household_num, reel_number, page_number, relationship, county
        FROM census_ocr_georgia_1870
        WHERE LOWER(county) = LOWER(?) AND is_black = 1
        ORDER BY {order}
        LIMIT ? OFFSET ?
    """, (county, per_page, offset)).fetchall()

    conn.close()
    pages = max(1, ((total - 1) // per_page) + 1)
    return render_template("browse_county.html",
                           nav_page="explore",
                           county=county.title(), state="Georgia",
                           records=[dict(r) for r in records],
                           total=total, page=page,
                           per_page=per_page, pages=pages, sort=sort)


@app.route("/explore/georgia/<county>")
def explore_county(county):
    conn = sqlite3.connect(str(DB_PATH), timeout=5)

    # Top Black surnames in this county from GA pipeline
    surnames_pipe = conn.execute("""
        SELECT last_name, COUNT(*) as n, SUM(is_black) as blk
        FROM census_ocr_georgia_1870
        WHERE LOWER(county) = LOWER(?) AND is_black = 1
          AND last_name IS NOT NULL AND last_name != ''
        GROUP BY last_name
        ORDER BY n DESC
        LIMIT 60
    """, (county,)).fetchall()

    # Also from ocr_records (Liberty/McIntosh)
    surnames_ocr = conn.execute("""
        SELECT last_name_raw as last_name, COUNT(*) as n
        FROM ocr_records
        WHERE LOWER(county) = LOWER(?) AND race_code = 2
        GROUP BY last_name_raw
        ORDER BY n DESC
        LIMIT 30
    """, (county,)).fetchall()

    total_black = conn.execute(
        "SELECT SUM(is_black) FROM census_ocr_georgia_1870 WHERE LOWER(county)=LOWER(?)",
        (county,)
    ).fetchone()[0] or 0

    conn.close()
    return render_template("explore_county.html",
                           nav_page="explore",
                           county=county.title(),
                           state="Georgia",
                           surnames_pipe=surnames_pipe,
                           surnames_ocr=surnames_ocr,
                           total_black=total_black)


@app.route("/submission/<submission_id>")
def submission_status(submission_id):
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM verification_submissions WHERE submission_id=?",
            (submission_id,)
        ).fetchone()
        conn.close()
        if not row:
            return render_template("submission_status.html", submission=None, not_found=True)
        return render_template("submission_status.html", submission=dict(row), not_found=False)
    except Exception as e:
        return f"Error: {e}", 500


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if ADMIN_PASSWORD and pw == ADMIN_PASSWORD:
            session["admin_ok"] = True
            return redirect(request.args.get("next") or "/admin/submissions")
        error = "Incorrect password."
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_ok", None)
    return redirect("/")


@app.route("/admin/submissions")
@_require_admin
def admin_submissions():
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM verification_submissions ORDER BY submitted_at DESC
        """).fetchall()
        conn.close()
        counts = {
            "total":    len(rows),
            "pending":  sum(1 for r in rows if r["status"] == "pending"),
            "verified": sum(1 for r in rows if r["status"] == "verified"),
            "rejected": sum(1 for r in rows if r["status"] == "rejected"),
        }
        submissions = []
        for r in rows:
            s = dict(r)
            # Parse auto_candidates JSON so template can iterate it
            raw = s.get("auto_candidates")
            if raw:
                try:
                    s["auto_candidates_parsed"] = json.loads(raw)[:5]  # top 5 only
                except (json.JSONDecodeError, TypeError):
                    s["auto_candidates_parsed"] = []
            else:
                s["auto_candidates_parsed"] = []
            submissions.append(s)
        return render_template("admin_submissions.html",
                               submissions=submissions, counts=counts)
    except Exception as e:
        return f"Error: {e}", 500


@app.route("/admin/submissions/<submission_id>/approve", methods=["POST"])
def admin_approve(submission_id):
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    sub = conn.execute(
        "SELECT * FROM verification_submissions WHERE submission_id=?",
        (submission_id,)
    ).fetchone()
    if not sub:
        conn.close()
        return "Not found", 404

    sub = dict(sub)
    member_id = f"MEMBER-{sub['anc_last'].upper()}-{sub['member_name'].split()[0].upper()}-{sub.get('anc_year') or 'XXXX'}"

    conn.execute("""
        INSERT OR IGNORE INTO members
        (member_id, first_name, last_name, birth_year, birth_state, birth_county,
         race, notes, submitted_by, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        member_id,
        sub["member_name"].split()[0] if sub["member_name"] else "",
        sub["member_name"].split()[-1] if sub["member_name"] else "",
        int(sub["anc_year"]) if sub.get("anc_year") and str(sub["anc_year"]).isdigit() else None,
        sub.get("anc_state", ""),
        sub.get("anc_county", ""),
        "Black",
        sub.get("notes", ""),
        sub.get("member_name", ""),
        datetime.datetime.now().isoformat(),
    ))

    conn.execute("""
        INSERT OR IGNORE INTO lineage_ancestors
        (member_id, generation, relationship, first_name, last_name,
         birth_year, birth_state, birth_county, ipums_histid, verified, notes, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        member_id, 1, "Direct ancestor (1870)",
        sub.get("anc_first", ""), sub.get("anc_last", ""),
        int(sub["anc_year"]) if sub.get("anc_year") and str(sub["anc_year"]).isdigit() else None,
        sub.get("anc_state", ""), sub.get("anc_county", ""),
        sub.get("ipums_histid", ""),
        1,
        f"Verified from submission {submission_id}. OCR record: {sub.get('ocr_id','')}.",
        datetime.datetime.now().isoformat(),
    ))

    conn.execute(
        "UPDATE verification_submissions SET status='verified' WHERE submission_id=?",
        (submission_id,)
    )
    conn.commit()
    conn.close()
    return redirect("/admin/submissions")


@app.route("/admin/submissions/<submission_id>/reject", methods=["POST"])
def admin_reject(submission_id):
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute(
        "UPDATE verification_submissions SET status='rejected' WHERE submission_id=?",
        (submission_id,)
    )
    conn.commit()
    conn.close()
    return redirect("/admin/submissions")


@app.route("/preview-cert")
def preview_cert():
    """Provisional certificate preview from a search result — no verification required."""
    first      = request.args.get("first", "").strip()
    last       = request.args.get("last", "").strip()
    birth_year = request.args.get("year", "").strip()
    state      = request.args.get("state", "Georgia").strip()
    county     = request.args.get("county", "").strip()
    conf       = request.args.get("conf", "0")
    histid     = request.args.get("histid", "").strip()
    tier       = int(request.args.get("tier", "0") or 0)
    ocr_id     = request.args.get("ocr_id", "").strip()

    if not last:
        return "Missing ancestor name", 400

    tier_labels = {3: "Tier 3 — Linked across 1870, 1880, 1900 censuses",
                   2: "Tier 2 — Linked across two censuses",
                   1: "Tier 1 — Single census record",
                   0: "Pending verification"}

    surname_key = last.lower()
    enslaver = ENSLAVER_DB.get(surname_key)

    cert_id = hashlib.md5(f"{ocr_id or last}-{first}-{datetime.date.today()}".encode()).hexdigest()[:12].upper()
    today   = datetime.date.today().strftime("%B %d, %Y")

    return render_template("preview_cert.html",
                           first=first, last=last,
                           birth_year=birth_year, state=state, county=county,
                           confidence=int(conf or 0),
                           histid=histid, tier=tier,
                           tier_label=tier_labels.get(tier, "Pending"),
                           ocr_id=ocr_id,
                           enslaver=enslaver,
                           cert_id=cert_id, today=today)


@app.route("/verify/<cert_id>")
def verify_cert(cert_id):
    """Public verification endpoint — confirms certificate authenticity without exposing PII."""
    cert_id = cert_id.upper().strip()
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT c.cert_id, c.issued_date, c.revoked,
                   m.last_name, m.birth_state,
                   (SELECT COUNT(*) FROM lineage_ancestors a
                    WHERE a.member_id=m.member_id AND a.verified=1) as verified_gens
            FROM certificates c
            JOIN members m ON c.member_id = m.member_id
            WHERE c.cert_id = ?
        """, (cert_id,)).fetchone()
        conn.close()
        if not row:
            return render_template("verify_cert.html", valid=False, cert_id=cert_id)
        data = dict(row)
        data["valid"] = not bool(data["revoked"])
        return render_template("verify_cert.html", valid=data["valid"], cert=data)
    except Exception as e:
        return f"Verification error: {e}", 500


@app.route("/api/verify/<cert_id>")
def api_verify_cert(cert_id):
    cert_id = cert_id.upper().strip()
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        row = conn.execute("""
            SELECT c.cert_id, c.issued_date, c.revoked,
                   m.last_name, m.birth_state
            FROM certificates c JOIN members m ON c.member_id=m.member_id
            WHERE c.cert_id=?
        """, (cert_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({"valid": False, "cert_id": cert_id}), 404
        return jsonify({
            "valid": not bool(row[2]),
            "cert_id": row[0],
            "issued_date": row[1],
            "birth_state": row[4],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/submissions/<submission_id>/suggest")
@_require_admin
def admin_suggest(submission_id):
    """Run auto-suggest search for a pending submission and store top candidates."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    sub = conn.execute(
        "SELECT * FROM verification_submissions WHERE submission_id=?", (submission_id,)
    ).fetchone()
    if not sub:
        conn.close()
        return jsonify({"error": "not found"}), 404
    sub = dict(sub)
    results, _ = run_search(
        sub.get("anc_last", ""), sub.get("anc_first", ""),
        birth_year=int(sub["anc_year"]) if sub.get("anc_year") and str(sub.get("anc_year")).isdigit() else None,
        state=sub.get("anc_state", "Georgia"),
        county=sub.get("anc_county") or None,
        max_results=10,
    )
    candidates = [
        {
            "name": f"{r['match']['first_name']} {r['match']['last_name']}",
            "state": r["match"]["state"],
            "county": r["match"]["county"],
            "birth_year": r["match"].get("birth_year"),
            "confidence": r["confidence"],
            "tier": r["tier"],
            "ocr_id": r["match"]["ocr_id"],
            "source": r["match"]["source_table"],
        }
        for r in results[:10]
    ]
    conn.execute(
        "UPDATE verification_submissions SET auto_candidates=? WHERE submission_id=?",
        (json.dumps(candidates), submission_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"candidates": candidates, "count": len(candidates)})


@app.route("/packet/<member_id>")
@_require_admin
def download_packet(member_id):
    """Generate and serve a Lineage Verification Packet PDF for a member."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    try:
        from generate_packet import generate_packet
        out_dir = Path.home() / "Documents/phi-beta-alpha/output/packets"
        path = generate_packet(member_id, out_dir)
        if not path:
            abort(404)
        return send_file(str(path), mimetype="application/pdf",
                         as_attachment=True, download_name=path.name)
    except Exception as e:
        return f"Packet generation error: {e}", 500


@app.route("/api/bridge/<member_id>")
@_require_admin
def api_bridge_candidates(member_id):
    """Return Bridge Engine candidates for a member's pre-1870 ancestors."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT bc.candidate_id, bc.ancestor_id, bc.source_table, bc.source_id,
               bc.enslaver_id, bc.overall_score, bc.notes, bc.computed_at,
               la.first_name, la.last_name, la.birth_year, la.generation,
               e.first_name as enslaver_first, e.last_name as enslaver_last,
               e.county as enslaver_county
        FROM bridge_candidates bc
        JOIN lineage_ancestors la ON la.ancestor_id = bc.ancestor_id
        LEFT JOIN enslavers e ON e.enslaver_id = bc.enslaver_id
        WHERE bc.member_id=?
        ORDER BY bc.overall_score DESC
    """, (member_id,)).fetchall()
    conn.close()
    candidates = [dict(r) for r in rows]
    return jsonify({"member_id": member_id, "count": len(candidates), "candidates": candidates})


@app.route("/walk", methods=["GET", "POST"])
def walk_begin():
    """Start an Anchored Backward Walk from a living-verifiable anchor person."""
    bw = _bw()

    def _walk_error(msg):
        conn = bw.open_db()
        bw.ensure_schema(conn)
        walks = bw.list_walks(conn)
        conn.close()
        return render_template("walk_begin.html",
                               error=msg, walks=walks,
                               nav_page="walk",
                               TARGET_STATES=TARGET_STATES,
                               DECADE_LADDER=bw.DECADE_LADDER)

    if request.method == "POST":
        first_name   = request.form.get("first_name", "").strip()
        last_name    = request.form.get("last_name", "").strip()
        birth_year   = request.form.get("birth_year", "").strip()
        state        = request.form.get("state", "").strip()
        county       = request.form.get("county", "").strip()
        start_decade = int(request.form.get("start_decade", 1950))
        verified_by  = request.form.get("verified_by", "").strip()
        member_id    = request.form.get("member_id", "").strip() or None

        if not all([first_name, last_name, birth_year, state]):
            return _walk_error("First name, last name, birth year, and state are required.")

        try:
            birth_year = int(birth_year)
        except ValueError:
            return _walk_error("Birth year must be a number.")

        anchor = {
            "first_name":   first_name,
            "last_name":    last_name,
            "birth_year":   birth_year,
            "state":        state,
            "county":       county or None,
            "start_decade": start_decade,
            "verified_by":  verified_by or "unspecified",
            "confidence":   100,
            "source_table": "manual",
            "source_id":    "anchor",
        }

        conn = bw.open_db()
        bw.ensure_schema(conn)

        fs_client = bw.WalkFamilySearchClient()
        chain = bw.run_walk(anchor, conn, fs_client=fs_client if fs_client.available else None)
        chain_id = bw.save_walk(chain, conn, member_id=member_id)
        conn.close()

        return redirect(url_for("walk_view", chain_id=chain_id))

    # GET — show form + list of existing walks
    conn = bw.open_db()
    bw.ensure_schema(conn)
    walks = bw.list_walks(conn)
    conn.close()

    prefill = {
        "first_name": request.args.get("first", ""),
        "last_name":  request.args.get("last", ""),
        "birth_year": request.args.get("year", ""),
        "state":      request.args.get("state", "Georgia"),
        "county":     request.args.get("county", ""),
    }
    return render_template("walk_begin.html",
                           walks=walks,
                           nav_page="walk",
                           prefill=prefill,
                           TARGET_STATES=TARGET_STATES,
                           DECADE_LADDER=bw.DECADE_LADDER)


@app.route("/walk/<chain_id>")
def walk_view(chain_id):
    """View a completed Anchored Backward Walk chain."""
    bw = _bw()
    conn = bw.open_db()
    bw.ensure_schema(conn)
    chain = bw.load_walk(chain_id, conn)
    conn.close()

    if not chain:
        abort(404)
    return render_template("walk_chain.html", chain=chain, nav_page="walk")


@app.route("/walk/<chain_id>/certificate")
def walk_certificate(chain_id):
    """Printable lineage certificate for an Anchored Backward Walk chain."""
    bw = _bw()
    conn = bw.open_db()
    bw.ensure_schema(conn)
    chain = bw.load_walk(chain_id, conn)
    conn.close()

    if not chain:
        abort(404)

    cert_id     = hashlib.md5(f"walk-{chain_id}".encode()).hexdigest()[:12].upper()
    today       = datetime.date.today().strftime("%B %d, %Y")
    verify_url  = request.host_url.rstrip("/") + f"/walk/{chain_id}"
    qr_data_uri = _make_qr_data_uri(verify_url)

    surname_key = (chain.get("anchor", {}).get("last_name") or "").lower()
    enslaver    = ENSLAVER_DB.get(surname_key)

    return render_template(
        "walk_cert.html",
        chain=chain,
        cert_id=cert_id,
        today=today,
        verify_url=verify_url,
        qr_data_uri=qr_data_uri,
        enslaver=enslaver,
        nav_page="walk",
    )


@app.route("/api/walk/<chain_id>")
def api_walk(chain_id):
    """JSON endpoint for a walk chain."""
    bw = _bw()
    conn = bw.open_db()
    bw.ensure_schema(conn)
    chain = bw.load_walk(chain_id, conn)
    conn.close()

    if not chain:
        abort(404)
    return jsonify(chain)


if __name__ == "__main__":
    app.run(debug=True, port=5050)
