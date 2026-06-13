#!/usr/bin/env python3
"""
generate_packet.py — Phi Beta Alpha Lineage Verification Packet

Produces a citation-complete PDF verification dossier for a confirmed member.
Every ancestor link includes source citations, confidence tier, and stated gaps.
The packet is the enabling artifact for federal carve-out claims, chapter eligibility,
and reparations documentation.

Usage:
  python3 scripts/generate_packet.py --member MEMBER-BACON-CLIFTON-1906
  python3 scripts/generate_packet.py --member MEMBER-BADGER-TRACY
  python3 scripts/generate_packet.py --all
  python3 scripts/generate_packet.py --member MEMBER-BACON-CLIFTON-1906 --out output/packets/
"""

import io
import sys
import math
import json
import hashlib
import argparse
import datetime
import sqlite3
import textwrap
from pathlib import Path

import fitz          # PyMuPDF
import qrcode
import qrcode.image.pil
from PIL import Image as PILImage

# ── Constants ─────────────────────────────────────────────────────────────────
DB_PATH    = Path.home() / "Documents/phi-beta-alpha/processed/lineage_1870.db"
OUT_DIR    = Path.home() / "Documents/phi-beta-alpha/output/packets"
VERIFY_URL = "https://phihetaalpha.org/verify/"   # will be local /verify/ in dev

# ΦΒΑ brand colors (0-1 float, RGB)
BLUE    = (26/255,  58/255, 143/255)   # #1a3a8f
GOLD    = (201/255, 168/255, 76/255)   # #c9a84c
WHITE   = (1.0, 1.0, 1.0)
BLACK   = (0.0, 0.0, 0.0)
GRAY    = (0.4, 0.4, 0.4)
LGRAY   = (0.93, 0.93, 0.93)
DGRAY   = (0.2, 0.2, 0.2)
RED_DIM = (0.55, 0.1, 0.1)
GREEN   = (0.1, 0.45, 0.2)
AMBER   = (0.7, 0.45, 0.0)

# Page geometry (US Letter: 612 × 792 pts)
W, H    = 612, 792
MARGIN  = 56
INNER_W = W - 2 * MARGIN  # 500 pts


# ── Font helpers ───────────────────────────────────────────────────────────────
def _text(page, x, y, s, size=10, color=BLACK, bold=False):
    fname = "helv" if not bold else "hebo"
    page.insert_text((x, y), s, fontname=fname, fontsize=size, color=color)


def _textbox(page, rect, s, size=10, color=BLACK, bold=False, align=0):
    fname = "helv" if not bold else "hebo"
    overflow = page.insert_textbox(
        rect, s, fontname=fname, fontsize=size, color=color, align=align
    )
    return overflow  # positive = chars that didn't fit


def _rect(page, r, fill=None, color=None, width=0.5):
    page.draw_rect(fitz.Rect(*r), color=color, fill=fill, width=width)


def _line(page, x1, y1, x2, y2, color=GOLD, width=1.0):
    page.draw_line(fitz.Point(x1, y1), fitz.Point(x2, y2), color=color, width=width)


# ── QR code ───────────────────────────────────────────────────────────────────
def _make_qr_png(url: str, size_px: int = 120) -> bytes:
    qr = qrcode.QRCode(box_size=4, border=2,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1a3a8f", back_color="white")
    img = img.resize((size_px, size_px), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _insert_qr(page, url: str, x: float, y: float, side: float = 80):
    png = _make_qr_png(url, size_px=int(side * 3))
    rect = fitz.Rect(x, y, x + side, y + side)
    page.insert_image(rect, stream=png)


# ── Confidence helpers ─────────────────────────────────────────────────────────
TIER_LABELS = {
    "Tier-3": ("TIER 3 — LINKED", GREEN),
    "Tier-2": ("TIER 2 — CONNECTED", AMBER),
    "Tier-1": ("TIER 1 — DOCUMENTED", AMBER),
    None:     ("UNVERIFIED",        RED_DIM),
}

STATUS_LABELS = {
    1: ("VERIFIED",  GREEN),
    0: ("UNVERIFIED", RED_DIM),
}


def _ancestor_confidence(anc: dict) -> int:
    """Rough per-ancestor confidence (0–100) based on sources and tier."""
    score = 0
    if anc["verified"]:
        score += 50
    tier = anc.get("ipums_tier")
    if tier == "Tier-3":
        score += 35
    elif tier == "Tier-2":
        score += 25
    elif tier == "Tier-1":
        score += 15
    if anc.get("ipums_histid"):
        score += 5
    if anc.get("fs_household_id") or anc.get("fs_source"):
        score += 8
    if anc.get("ocr_image"):
        score += 5
    return min(score, 100)


def _packet_confidence(ancestors: list[dict]) -> int:
    """Overall packet confidence — weighted by generation (earlier gens count more)."""
    if not ancestors:
        return 0
    verified = [a for a in ancestors if a["verified"]]
    if not verified:
        return 0
    total_w = 0.0
    total_score = 0.0
    for anc in ancestors:
        gen = anc.get("generation", 1)
        weight = 1.0 / gen  # earlier generations carry more weight
        total_w += weight
        total_score += weight * _ancestor_confidence(anc)
    return int(total_score / total_w) if total_w else 0


# ── Data access ───────────────────────────────────────────────────────────────
def _load_member(conn, member_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM members WHERE member_id=?", (member_id,)).fetchone()
    if not row:
        return None
    cols = [d[1] for d in conn.execute("PRAGMA table_info(members)").fetchall()]
    return dict(zip(cols, row))


def _load_ancestors(conn, member_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM lineage_ancestors WHERE member_id=? ORDER BY generation",
        (member_id,)
    ).fetchall()
    cols = [d[1] for d in conn.execute("PRAGMA table_info(lineage_ancestors)").fetchall()]
    return [dict(zip(cols, r)) for r in rows]


def _load_cert(conn, member_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM certificates WHERE member_id=?",
                       (member_id,)).fetchone()
    if not row:
        return None
    cols = [d[1] for d in conn.execute("PRAGMA table_info(certificates)").fetchall()]
    return dict(zip(cols, row))


# ── PDF building blocks ────────────────────────────────────────────────────────

def _draw_header_bar(page, title_line2: str = "LINEAGE VERIFICATION PACKET"):
    """Blue header bar with ΦΒΑ name and document type."""
    _rect(page, [0, 0, W, 80], fill=BLUE)
    _text(page, MARGIN, 30, "Φ Β Α", size=20, color=GOLD, bold=True)
    _text(page, MARGIN + 50, 30, "Phi Beta Alpha Historical Lineage Society",
          size=11, color=WHITE, bold=True)
    _text(page, MARGIN + 50, 50, title_line2, size=9, color=(0.8, 0.85, 1.0))
    _line(page, 0, 80, W, 80, color=GOLD, width=2)


def _draw_footer(page, page_num: int, total_pages: int, cert_id: str):
    """Gold rule + cert ID + page number."""
    _line(page, MARGIN, H - 45, W - MARGIN, H - 45, color=GOLD, width=0.8)
    _text(page, MARGIN, H - 30, f"CERT # {cert_id}", size=7, color=GRAY)
    _text(page, MARGIN, H - 20,
          "This document is a certification artifact of Phi Beta Alpha Historical Lineage Society. "
          "Confidence scores reflect evidentiary weight, not certainty.",
          size=6.5, color=GRAY)
    _text(page, W - MARGIN - 40, H - 30, f"Page {page_num} of {total_pages}",
          size=7, color=GRAY)


def _draw_cover_page(doc: fitz.Document, member: dict, ancestors: list[dict],
                     cert: dict) -> None:
    page = doc.new_page(width=W, height=H)
    _draw_header_bar(page)

    cert_id  = cert["cert_id"] if cert else "PENDING"
    conf     = _packet_confidence(ancestors)
    n_ver    = sum(1 for a in ancestors if a["verified"])
    n_total  = len(ancestors)
    issued   = (cert["issued_date"] if cert else datetime.date.today().isoformat())

    # ── Verification badge ─────────────────────────────────────────────────
    badge_y = 105
    badge_color = GREEN if n_ver >= 3 else (AMBER if n_ver >= 1 else RED_DIM)
    _rect(page, [MARGIN, badge_y, MARGIN + 180, badge_y + 26], fill=badge_color)
    status_text = "LINEAGE VERIFIED" if n_ver >= 3 else ("PARTIAL VERIFICATION" if n_ver >= 1 else "PENDING REVIEW")
    _text(page, MARGIN + 8, badge_y + 17, status_text, size=10, color=WHITE, bold=True)

    # ── Member name ────────────────────────────────────────────────────────
    full_name = f"{member['first_name']} {member['last_name']}"
    _text(page, MARGIN, badge_y + 55, full_name, size=22, color=BLUE, bold=True)
    loc = f"{member.get('birth_county') or ''}{', ' if member.get('birth_county') and member.get('birth_state') else ''}{member.get('birth_state') or ''}".strip(", ")
    if loc:
        _text(page, MARGIN, badge_y + 75, loc, size=11, color=GRAY)

    # ── Metadata table ─────────────────────────────────────────────────────
    meta_y = badge_y + 100
    _line(page, MARGIN, meta_y, W - MARGIN, meta_y, color=LGRAY, width=0.5)
    meta_items = [
        ("Certification #",   cert_id),
        ("Issue Date",        issued),
        ("Generations Documented", f"{n_total}"),
        ("Generations Verified",   f"{n_ver} of {n_total}"),
        ("Overall Confidence",     f"{conf}/100"),
        ("Society",               "Phi Beta Alpha Historical Lineage Society"),
        ("EIN",                   "41-4969999"),
    ]
    row_h = 22
    for i, (label, val) in enumerate(meta_items):
        y = meta_y + 12 + i * row_h
        _text(page, MARGIN, y, label, size=8.5, color=GRAY)
        _text(page, MARGIN + 165, y, val, size=9, color=BLACK, bold=True)

    # ── Confidence bar ─────────────────────────────────────────────────────
    bar_y = meta_y + 12 + len(meta_items) * row_h + 15
    _text(page, MARGIN, bar_y, "Evidence Strength", size=8, color=GRAY)
    bar_w = INNER_W
    _rect(page, [MARGIN, bar_y + 8, MARGIN + bar_w, bar_y + 18], fill=LGRAY)
    fill_w = bar_w * conf / 100
    fill_color = GREEN if conf >= 70 else (AMBER if conf >= 40 else RED_DIM)
    _rect(page, [MARGIN, bar_y + 8, MARGIN + fill_w, bar_y + 18], fill=fill_color)
    _text(page, MARGIN + fill_w + 4, bar_y + 17, f"{conf}%", size=7, color=GRAY)

    # ── Ancestor chain summary ─────────────────────────────────────────────
    chain_y = bar_y + 40
    _text(page, MARGIN, chain_y, "DOCUMENTED ANCESTRY CHAIN",
          size=8, color=BLUE, bold=True)
    _line(page, MARGIN, chain_y + 6, W - MARGIN, chain_y + 6, color=GOLD, width=0.5)

    for i, anc in enumerate(ancestors):
        ay = chain_y + 18 + i * 28
        if ay > H - 120:
            break
        gen = anc.get("generation", i + 1)
        name = f"{anc['first_name']} {anc['last_name']}"
        yr   = f"~{anc['birth_year']}" if anc.get("birth_year") else ""
        loc  = f"{anc.get('birth_county') or ''}, {anc.get('birth_state') or ''}".strip(", ")
        rel  = anc.get("relationship", "").replace("-", " ").title()
        anc_conf = _ancestor_confidence(anc)

        # generation dot
        dot_x, dot_y = MARGIN + 6, ay + 7
        dot_fill = GREEN if anc["verified"] else RED_DIM
        page.draw_circle(fitz.Point(dot_x, dot_y), 5, color=dot_fill, fill=dot_fill)
        _text(page, dot_x - 3, dot_y + 3, str(gen), size=6, color=WHITE, bold=True)

        _text(page, MARGIN + 20, ay + 4,  name, size=10, color=BLACK, bold=True)
        _text(page, MARGIN + 20, ay + 16, f"{rel} · {yr} · {loc}", size=8, color=GRAY)
        # confidence badge
        conf_color = GREEN if anc_conf >= 70 else (AMBER if anc_conf >= 40 else RED_DIM)
        _rect(page, [W - MARGIN - 42, ay, W - MARGIN, ay + 14], fill=conf_color)
        _text(page, W - MARGIN - 36, ay + 10, f"{anc_conf}%", size=7.5, color=WHITE, bold=True)

        # connector line
        if i < len(ancestors) - 1 and ay + 28 < H - 120:
            _line(page, dot_x, dot_y + 5, dot_x, ay + 28, color=LGRAY, width=0.8)

    # ── QR code ───────────────────────────────────────────────────────────
    qr_url  = f"{VERIFY_URL}{cert_id}"
    qr_side = 75
    qr_x    = W - MARGIN - qr_side
    qr_y    = H - 130
    _insert_qr(page, qr_url, qr_x, qr_y, qr_side)
    _text(page, qr_x - 2, qr_y + qr_side + 10, "Verify online", size=6.5, color=GRAY)
    _textbox(page, fitz.Rect(qr_x - 2, qr_y + qr_side + 18, qr_x + qr_side + 4, qr_y + qr_side + 35),
             qr_url, size=5.5, color=GRAY)

    _draw_footer(page, 1, -1, cert_id)  # total pages set in postprocess


def _draw_evidence_pages(doc: fitz.Document, member: dict, ancestors: list[dict],
                          cert: dict, start_page_num: int) -> int:
    """One evidence section per ancestor. Returns number of pages added."""
    cert_id = cert["cert_id"] if cert else "PENDING"
    pages_added = 0

    for anc in ancestors:
        page = doc.new_page(width=W, height=H)
        _draw_header_bar(page, title_line2="EVIDENCE RECORD — ANCESTRY CHAIN")
        pages_added += 1

        y = 98

        # Generation + name banner
        gen  = anc.get("generation", "?")
        name = f"{anc['first_name']} {anc['last_name']}"
        rel  = anc.get("relationship", "").replace("-", " ").title()
        ver_text, ver_color = STATUS_LABELS.get(anc["verified"], ("UNVERIFIED", RED_DIM))

        _rect(page, [MARGIN, y, W - MARGIN, y + 30], fill=LGRAY)
        _text(page, MARGIN + 8, y + 10, f"GENERATION {gen}", size=7, color=BLUE, bold=True)
        _text(page, MARGIN + 8, y + 23, name, size=14, color=BLACK, bold=True)
        # status badge
        badge_w = 82
        _rect(page, [W - MARGIN - badge_w, y + 5, W - MARGIN - 2, y + 25], fill=ver_color)
        _text(page, W - MARGIN - badge_w + 6, y + 18, ver_text, size=8, color=WHITE, bold=True)

        y += 40
        _text(page, MARGIN, y, rel, size=9, color=GRAY)

        # Key fields table
        y += 18
        fields = [
            ("Birth Year",      f"~{anc['birth_year']}" if anc.get("birth_year") else anc.get("birth_year_est") or "Unknown"),
            ("Birth State",     anc.get("birth_state") or "Unknown"),
            ("Birth County",    anc.get("birth_county") or "Unknown"),
            ("Death Year",      str(anc["death_year"]) if anc.get("death_year") else "Unknown"),
            ("IPUMS HISTID",    anc.get("ipums_histid") or "—"),
            ("IPUMS Tier",      anc.get("ipums_tier") or "—"),
            ("FamilySearch HH", anc.get("fs_household_id") or "—"),
        ]
        col2_x = MARGIN + 160
        _line(page, MARGIN, y, W - MARGIN, y, color=GOLD, width=0.5)
        y += 8
        for label, val in fields:
            _text(page, MARGIN, y, label, size=8, color=GRAY)
            # truncate long values
            if len(val) > 55:
                val = val[:52] + "…"
            _text(page, col2_x, y, val, size=8.5, color=BLACK, bold=True)
            y += 16
        _line(page, MARGIN, y, W - MARGIN, y, color=LGRAY, width=0.3)
        y += 10

        # Confidence
        anc_conf = _ancestor_confidence(anc)
        conf_color = GREEN if anc_conf >= 70 else (AMBER if anc_conf >= 40 else RED_DIM)
        _text(page, MARGIN, y, f"Evidence Confidence: {anc_conf}/100", size=8.5,
              color=conf_color, bold=True)
        tier_label, _ = TIER_LABELS.get(anc.get("ipums_tier"), TIER_LABELS[None])
        _text(page, MARGIN + 200, y, tier_label, size=8, color=GRAY)
        y += 20

        # FamilySearch source
        fs_src = anc.get("fs_source") or ""
        if fs_src:
            _text(page, MARGIN, y, "PRIMARY SOURCE", size=7.5, color=BLUE, bold=True)
            y += 12
            # word-wrap source citation
            wrap = textwrap.fill(fs_src, width=90)
            for ln in wrap.splitlines():
                _text(page, MARGIN + 12, y, ln, size=7.5, color=DGRAY)
                y += 12

        y += 6
        _line(page, MARGIN, y, W - MARGIN, y, color=LGRAY, width=0.3)
        y += 10

        # Notes / evidence narrative
        notes = anc.get("notes") or ""
        if notes:
            _text(page, MARGIN, y, "EVIDENCE NARRATIVE", size=7.5, color=BLUE, bold=True)
            y += 12
            # word-wrap notes
            wrap = textwrap.fill(notes, width=88)
            for ln in wrap.splitlines():
                if y > H - 75:
                    _text(page, MARGIN + 12, y, "[continued — see full DB record]",
                          size=7, color=GRAY)
                    break
                _text(page, MARGIN + 12, y, ln, size=7.5, color=DGRAY)
                y += 12

        y += 10
        _line(page, MARGIN, y, W - MARGIN, y, color=LGRAY, width=0.3)
        y += 10

        # OCR image reference
        if anc.get("ocr_image"):
            _text(page, MARGIN, y, f"Census Image: {anc['ocr_image']}", size=7.5, color=GRAY)

        _draw_footer(page, start_page_num + pages_added - 1, -1, cert_id)

    return pages_added


def _draw_attestation_page(doc: fitz.Document, member: dict, ancestors: list[dict],
                            cert: dict) -> None:
    """Final page: verification statement, gaps, disclaimer."""
    page = doc.new_page(width=W, height=H)
    _draw_header_bar(page, title_line2="VERIFICATION ATTESTATION")
    cert_id = cert["cert_id"] if cert else "PENDING"
    conf    = _packet_confidence(ancestors)
    n_ver   = sum(1 for a in ancestors if a["verified"])
    n_unver = len(ancestors) - n_ver

    y = 105
    _text(page, MARGIN, y, "ATTESTATION", size=13, color=BLUE, bold=True)
    _line(page, MARGIN, y + 8, W - MARGIN, y + 8, color=GOLD, width=1.0)
    y += 24

    attestation = (
        f"Phi Beta Alpha Historical Lineage Society (EIN 41-4969999) has reviewed "
        f"the documentary evidence presented for {member['first_name']} {member['last_name']} "
        f"and documented {n_ver} verified ancestor link{'s' if n_ver != 1 else ''} "
        f"in the lineage chain above. "
        f"Overall evidence confidence: {conf}/100."
    )
    for ln in textwrap.fill(attestation, width=88).splitlines():
        _text(page, MARGIN, y, ln, size=9, color=BLACK)
        y += 14
    y += 10

    # Gaps and conflicts
    _text(page, MARGIN, y, "STATED GAPS & LIMITATIONS", size=8, color=RED_DIM, bold=True)
    y += 14
    gaps = []
    for anc in ancestors:
        if not anc["verified"]:
            g = (f"Generation {anc['generation']}: {anc['first_name']} {anc['last_name']} — "
                 f"{anc.get('relationship','').replace('-',' ').title()} — status UNVERIFIED. "
                 f"Evidence pending.")
            gaps.append(g)
        # look for explicit gap notes
        notes = anc.get("notes") or ""
        if "UNCONFIRMED" in notes or "MISSING" in notes or "pending" in notes.lower():
            gaps.append(f"Generation {anc['generation']}: {anc['first_name']} {anc['last_name']} — see evidence narrative for open questions.")
    if not gaps:
        gaps.append("No significant gaps identified in this packet. Reviewed record chain is continuous.")
    for g in gaps:
        for ln in textwrap.fill(f"• {g}", width=90).splitlines():
            if y > H - 130:
                break
            _text(page, MARGIN + (12 if not ln.startswith("•") else 0), y, ln, size=7.5, color=DGRAY)
            y += 12
    y += 12

    # Disclaimer box
    _rect(page, [MARGIN, y, W - MARGIN, y + 70], fill=LGRAY)
    disclaimer = (
        "DISCLAIMER: This packet presents evidence and confidence scores based on available primary source "
        "documents. Confidence scores reflect evidentiary weight, not legal certainty. This document is "
        "intended for chapter eligibility, advocacy, and reparations documentation purposes and does not "
        "constitute legal advice. Records cited are from NARA microfilm (M593), IPUMS-USA linked datasets, "
        "FamilySearch, and the ΦΒΑ OCR corpus. Gaps are stated openly; this document does not overclaim."
    )
    _textbox(page, fitz.Rect(MARGIN + 8, y + 6, W - MARGIN - 8, y + 64),
             disclaimer, size=7, color=DGRAY)
    y += 80

    # Verification URL
    qr_url = f"{VERIFY_URL}{cert_id}"
    _text(page, MARGIN, y, "Digital Verification:", size=8, color=BLUE, bold=True)
    _text(page, MARGIN + 110, y, qr_url, size=8, color=BLUE)
    y += 16
    _text(page, MARGIN, y, f"Issued: {cert['issued_date'] if cert else 'pending'}", size=8, color=GRAY)
    _text(page, MARGIN + 160, y, f"Cert ID: {cert_id}", size=8, color=GRAY)

    # QR in bottom-right
    _insert_qr(page, qr_url, W - MARGIN - 72, H - 140, side=72)
    _draw_footer(page, -1, -1, cert_id)  # patched later


# ── Total page count patcher ───────────────────────────────────────────────────

def _patch_page_counts(doc: fitz.Document) -> None:
    """Replace -1 placeholders with actual total page count."""
    total = doc.page_count
    for pnum in range(total):
        page = doc[pnum]
        # Get the current page dictionary and redact "-1" strings in footer
        # Strategy: search for placeholder text and replace via annotation redaction
        # Simpler: rebuild footer text in-place
        # Since we wrote page numbers as -1, find and update them
        # Use page.get_text to find instances and page.add_redact_annot
        instances = page.search_for("of -1")
        for inst in instances:
            page.add_redact_annot(inst, text=f"of {total}", fontsize=7,
                                  fill=(1, 1, 1), text_color=GRAY)
        instances2 = page.search_for("Page -1")
        for inst in instances2:
            page.add_redact_annot(inst, text=f"Page {pnum + 1}", fontsize=7,
                                  fill=(1, 1, 1), text_color=GRAY)
        page.apply_redactions()


# ── Main entry ─────────────────────────────────────────────────────────────────

def generate_packet(member_id: str, out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)

    member    = _load_member(conn, member_id)
    if not member:
        print(f"Member not found: {member_id}")
        conn.close()
        return None

    ancestors = _load_ancestors(conn, member_id)
    cert      = _load_cert(conn, member_id)
    conn.close()

    doc = fitz.open()

    # Page 1: cover
    _draw_cover_page(doc, member, ancestors, cert)

    # Pages 2+N: one per ancestor
    _draw_evidence_pages(doc, member, ancestors, cert, start_page_num=2)

    # Final page: attestation
    _draw_attestation_page(doc, member, ancestors, cert)

    # Patch total page counts
    _patch_page_counts(doc)

    safe_name  = f"{member['last_name']}_{member['first_name']}".replace(" ", "_")
    cert_id    = cert["cert_id"] if cert else "PENDING"
    filename   = f"PBA_Packet_{safe_name}_{cert_id[:8]}.pdf"
    out_path   = out_dir / filename
    doc.save(str(out_path), garbage=4, deflate=True)
    doc.close()

    size_kb = out_path.stat().st_size // 1024
    print(f"  Generated: {out_path} ({size_kb} KB)")
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Generate ΦΒΑ Lineage Verification Packet PDF"
    )
    parser.add_argument("--member", help="Member ID (e.g. MEMBER-BACON-CLIFTON-1906)")
    parser.add_argument("--all",    action="store_true", help="Generate for all members")
    parser.add_argument("--out",    default=str(OUT_DIR), help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.out)

    if args.all or not args.member:
        conn    = sqlite3.connect(str(DB_PATH), timeout=10)
        members = [r[0] for r in conn.execute("SELECT member_id FROM members").fetchall()]
        conn.close()
        for mid in members:
            generate_packet(mid, out_dir)
    else:
        generate_packet(args.member, out_dir)


if __name__ == "__main__":
    main()
