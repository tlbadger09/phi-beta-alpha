#!/usr/bin/env python3
"""
backward_walk.py — Anchored Backward Walk Engine

Starting from a living-verifiable anchor (typically 1950), walks one rope
backward through census decades to 1870, then hands off to the Bridge Engine.

Each decade step scores candidates on 6 signals (per spec):
  Name (25) + Age progression (20) + Birthplace (15) +
  Household continuity (20) + Geographic plausibility (15) + Sex/race (5)

IPUMS linkage (1870/1880/1900) provides independent validation bonus.
Chain product and weakest link are computed and stored for the Verification Packet.

Usage (CLI):
  python3 scripts/backward_walk.py --test-bacon
  python3 scripts/backward_walk.py --first Clifton --last Bacon --birthyr 1907 \
          --state Georgia --county McIntosh --start-decade 1950
  python3 scripts/backward_walk.py --show <chain_id>
  python3 scripts/backward_walk.py --report
"""

import json
import math
import sys
import argparse
import hashlib
import sqlite3
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "lineage_check"))

from member_lookup import (
    soundex, normalize_str, STATE_FIPS, GA_COUNTY_NHGIS,
    county_nhgis, query_ipums, infer_sex, FamilySearchClient,
)
from search import search_ocr_by_name, get_household, open_db as open_search_db

DB_PATH = Path.home() / "Documents/phi-beta-alpha/processed/lineage_1870.db"

# ── Constants ──────────────────────────────────────────────────────────────────

DECADE_LADDER = [1950, 1940, 1930, 1920, 1910, 1900, 1880, 1870]
# Note: 1890 deliberately absent — ~99% of records destroyed in 1921 fire.
# The 1900↔1880 gap is 20 years; widen slop accordingly.

FS_COLLECTION_IDS = {
    1950: "4452532",
    1940: "2000219",
    1930: "1937053",
    1920: "1488411",
    1910: "1727033",
    1900: "1325221",
    1880: "1417683",
    1870: "1438024",
}

# Known Great Migration corridors: origin state → plausible destination states post-1910
MIGRATION_CORRIDORS = {
    "georgia":        {"illinois", "new york", "ohio", "michigan", "pennsylvania", "indiana"},
    "south carolina": {"new york", "pennsylvania", "new jersey", "ohio", "connecticut"},
    "mississippi":    {"illinois", "michigan", "ohio", "california", "indiana"},
    "alabama":        {"illinois", "ohio", "michigan", "california", "indiana"},
    "virginia":       {"new york", "pennsylvania", "new jersey", "maryland", "district of columbia"},
    "north carolina": {"new york", "pennsylvania", "new jersey", "ohio"},
    "louisiana":      {"california", "illinois", "texas", "new york"},
    "tennessee":      {"illinois", "ohio", "michigan", "indiana"},
}

MIN_LINK_SCORE = 20       # minimum to store a link; below = gap
IPUMS_BONUS    = 10       # added to link score when IPUMS independently confirms


# ── DB schema ─────────────────────────────────────────────────────────────────

def open_db() -> sqlite3.Connection:
    """Open the lineage DB with the row_factory and custom functions search.py expects."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.create_function("soundex_py", 1, soundex)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS walk_chains (
            chain_id            TEXT PRIMARY KEY,
            member_id           TEXT,
            anchor_first_name   TEXT,
            anchor_last_name    TEXT,
            anchor_birth_year   INTEGER,
            anchor_state        TEXT,
            anchor_county       TEXT,
            anchor_start_decade INTEGER DEFAULT 1950,
            anchor_verified_by  TEXT,
            chain_product       REAL DEFAULT 0,
            weakest_link_decade INTEGER,
            weakest_link_score  INTEGER,
            status              TEXT DEFAULT 'draft',
            created_at          TEXT DEFAULT (datetime('now')),
            notes               TEXT
        );

        CREATE TABLE IF NOT EXISTS walk_links (
            link_id           TEXT PRIMARY KEY,
            chain_id          TEXT NOT NULL REFERENCES walk_chains(chain_id),
            step_order        INTEGER,
            decade            INTEGER NOT NULL,
            first_name        TEXT,
            last_name         TEXT,
            birth_year        INTEGER,
            birth_month       INTEGER,
            state             TEXT,
            county            TEXT,
            sex               TEXT,
            race              TEXT,
            age_at_census     INTEGER,
            source_table      TEXT,
            source_id         TEXT,
            confidence        INTEGER DEFAULT 0,
            ipums_validated   INTEGER DEFAULT 0,
            ipums_histid      TEXT,
            link_tier         TEXT DEFAULT 'census',
            runners_up        TEXT,
            score_breakdown   TEXT,
            is_gap            INTEGER DEFAULT 0,
            created_at        TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()


# ── Name utilities ────────────────────────────────────────────────────────────

def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for ca in a:
        curr = [prev[0] + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (0 if ca == cb else 1)))
        prev = curr
    return prev[-1]


def _name_sim(a: str, b: str) -> float:
    if not a and not b:
        return 0.6
    if not a or not b:
        return 0.4
    a, b = a.lower(), b.lower()
    if a == b:
        return 1.0
    return max(0.0, 1.0 - _levenshtein(a, b) / max(len(a), len(b)))


# ── Scoring components ────────────────────────────────────────────────────────

def _score_name(current: dict, candidate: dict) -> int:
    """Name match: 0-25 pts. Tolerates Soundex variants, nicknames, abbreviations."""
    c_first = current.get("first_name", "") or ""
    c_last  = current.get("last_name", "")  or ""
    k_first = candidate.get("first_name", "") or ""
    k_last  = candidate.get("last_name", "")  or ""

    last_sim  = _name_sim(c_last, k_last)
    first_sim = _name_sim(c_first, k_first)

    # Soundex bonus: both names hash the same = strong phonetic match
    sdx_last_match  = soundex(c_last)  == soundex(k_last)  if c_last and k_last else False
    sdx_first_match = soundex(c_first) == soundex(k_first) if c_first and k_first else False

    last_score  = last_sim * 15 + (3 if sdx_last_match else 0)
    first_score = first_sim * 7

    return min(25, round(last_score + first_score))


def _score_age_progression(current: dict, candidate: dict, decade_gap: int) -> int:
    """
    Age progression: 0-20 pts.
    The candidate is decade_gap years earlier, so their age should be ~gap less.
    Slop: ±3 for 10-year gap; ±5 for 20-year gap (1900↔1880 bridge over missing 1890).
    """
    c_birth = current.get("birth_year")
    k_birth = candidate.get("birth_year")

    if c_birth and k_birth:
        # Birth year should be the same person — same birth year (modulo age slop)
        diff = abs(c_birth - k_birth)
        slop = 5 if decade_gap == 20 else 3
        if diff <= slop:
            return 20
        if diff <= slop + 2:
            return 14
        if diff <= slop + 5:
            return 8
        return 0

    # Fall back to age field in census record
    c_age = current.get("age_at_census") or current.get("age")
    k_age = candidate.get("age_at_census") or candidate.get("age")
    if c_age is None or k_age is None:
        return 8  # neutral
    try:
        c_age, k_age = int(c_age), int(k_age)
    except (ValueError, TypeError):
        return 8

    expected_k_age = c_age - decade_gap
    diff = abs(k_age - expected_k_age)
    slop = 5 if decade_gap == 20 else 3

    if diff <= slop:
        return 20
    if diff <= slop + 2:
        return 14
    if diff <= slop + 5:
        return 8
    return 0


def _score_birthplace(current: dict, candidate: dict) -> int:
    """Birthplace (state): 0-15 pts. Birthplace is where you were BORN, stable across decades."""
    c_bpl = normalize_str(current.get("birthplace") or current.get("state") or "")
    k_bpl = normalize_str(candidate.get("birthplace") or candidate.get("state") or "")
    if not c_bpl or not k_bpl:
        return 7  # neutral
    if c_bpl == k_bpl:
        return 15
    # Partial credit for same region (both Deep South states, both Northeast, etc.)
    deep_south = {"georgia", "south carolina", "north carolina", "alabama",
                  "mississippi", "louisiana", "virginia", "tennessee", "florida"}
    if c_bpl in deep_south and k_bpl in deep_south:
        return 8
    northeast = {"new york", "pennsylvania", "new jersey", "connecticut", "massachusetts"}
    if c_bpl in northeast and k_bpl in northeast:
        return 8
    return 2


def _score_household(current: dict, candidate: dict) -> int:
    """
    Household continuity: 0-20 pts.
    Checks if spouse, children, or parents are present in both records.
    When household data is unavailable (FamilySearch without full fetch), returns 0.
    """
    c_hh = current.get("household") or []
    k_hh = candidate.get("household") or []
    if not c_hh or not k_hh:
        return 0  # honest zero — no data, no points

    # Build name sets for comparison
    def hh_names(members):
        names = set()
        for m in members:
            name = m.get("name", "")
            if name:
                parts = name.strip().split()
                if parts:
                    names.add(soundex(parts[-1]))  # Soundex last name
        return names

    c_names = hh_names(c_hh)
    k_names = hh_names(k_hh)
    shared  = len(c_names & k_names)

    if shared >= 2:
        return 20
    if shared == 1:
        return 12
    return 0


def _score_geography(current: dict, candidate: dict, decade_gap: int) -> int:
    """
    Geographic plausibility: 0-15 pts.
    Same county = full. Post-1910 migration corridor = near-full. Unknown shift = near-zero.
    """
    c_state  = normalize_str(current.get("state") or "")
    k_state  = normalize_str(candidate.get("state") or "")
    c_county = normalize_str(current.get("county") or "")
    k_county = normalize_str(candidate.get("county") or "")

    if not c_state or not k_state:
        return 5  # neutral

    state_match  = c_state == k_state
    county_match = c_county and k_county and c_county == k_county

    if state_match and county_match:
        return 15
    if state_match:
        return 10

    # Different state — check migration corridor
    origin = k_state  # candidate is earlier = origin state
    dest   = c_state  # current is later = destination
    corridor = MIGRATION_CORRIDORS.get(origin, set())
    if dest in corridor:
        return 12  # known Great Migration path — plausible

    return 2


def _score_sex_race(current: dict, candidate: dict) -> int:
    """Sex/race consistency: 0-5 pts. Race flip is flagged, not failed."""
    c_sex = (current.get("sex") or "").upper()[:1]
    k_sex = (candidate.get("sex") or "").upper()[:1]

    if not c_sex or not k_sex:
        return 3  # neutral
    if c_sex == k_sex:
        return 5
    return 0  # mismatch


def score_link(current: dict, candidate: dict, decade_gap: int = 10) -> dict:
    """
    Score a candidate as 'same person as current, decade_gap years earlier'.
    Returns {score: 0-100, breakdown: {component: pts}, flags: [...]}
    """
    bd = {
        "name":       _score_name(current, candidate),
        "age":        _score_age_progression(current, candidate, decade_gap),
        "birthplace": _score_birthplace(current, candidate),
        "household":  _score_household(current, candidate),
        "geography":  _score_geography(current, candidate, decade_gap),
        "sex_race":   _score_sex_race(current, candidate),
    }
    flags = []
    # Flag race field change (not a penalty, just metadata)
    c_race = (current.get("race") or "").upper()[:1]
    k_race = (candidate.get("race") or "").upper()[:1]
    if c_race and k_race and c_race != k_race:
        flags.append(f"race_flip:{k_race}→{c_race}")

    return {"score": min(100, sum(bd.values())), "breakdown": bd, "flags": flags}


# ── IPUMS validation ──────────────────────────────────────────────────────────

def check_ipums_validation(conn: sqlite3.Connection,
                            person: dict, target_decade: int) -> dict | None:
    """
    Check whether an IPUMS record in target_decade independently links to
    adjacent census decades, confirming the person's cross-decade identity.

    person: dict with birth_year, state, county (optional), sex
    target_decade: 1870, 1880, or 1900

    Returns {histid, link_forward, link_back, bonus} or None.
    """
    if target_decade not in (1870, 1880, 1900):
        return None

    birth_year = person.get("birth_year")
    if not birth_year:
        return None

    def _extract_state(raw: str) -> str:
        """Pull the state name from a string that may include county or country."""
        raw = normalize_str((raw or "").replace("united states", "").replace("usa", ""))
        # Try known states directly
        for candidate in [raw] + [p.strip() for p in raw.replace(",", " ").split()]:
            if candidate in STATE_FIPS:
                return candidate
        # Multi-word state names
        parts = [p.strip() for p in raw.split(",")]
        for p in reversed(parts):
            p = p.strip()
            if p in STATE_FIPS:
                return p
        return raw.split(",")[-1].strip()

    # For pre-migration records (1870-1900), birth state is more accurate than
    # current residence for IPUMS lookup (family hadn't yet migrated north/GA)
    bp_raw = person.get("birthplace") or ""
    bp_state = _extract_state(bp_raw) if bp_raw else ""
    res_state = _extract_state(person.get("state") or "")

    state_key = bp_state if (bp_state and bp_state in STATE_FIPS) else res_state
    statefip  = STATE_FIPS.get(state_key)
    if not statefip:
        return None

    county_code = None
    if statefip == 13:
        county_code = GA_COUNTY_NHGIS.get(normalize_str(person.get("county") or ""))

    sex_int = 1 if (person.get("sex") or "").upper()[:1] == "M" else \
              2 if (person.get("sex") or "").upper()[:1] == "F" else 0

    slop = 3
    conditions = [
        "YEAR = ?", "STATEFIP = ?", "RACE = 2",
        "BIRTHYR BETWEEN ? AND ?",
    ]
    params = [target_decade, statefip, birth_year - slop, birth_year + slop]

    if county_code:
        conditions.append("COUNTYNHG = ?")
        params.append(county_code)
    if sex_int in (1, 2):
        conditions.append("SEX = ?")
        params.append(sex_int)

    sql = f"""
        SELECT HISTID, LINK1870, LINK1880, LINK1900
        FROM census
        WHERE {' AND '.join(conditions)}
        ORDER BY ABS(BIRTHYR - ?) LIMIT 1
    """
    params.append(birth_year)
    row = conn.execute(sql, params).fetchone()
    if not row:
        return None

    histid, l70, l80, l100 = row
    links_set = sum([1 for x in [l70, l80, l100] if x])
    if links_set < 2:
        return None  # only linked in 1 census — not strong enough for bonus

    return {
        "histid":       histid,
        "link1870":     l70,
        "link1880":     l80,
        "link1900":     l100,
        "links_set":    links_set,
        "bonus":        IPUMS_BONUS,
    }


# ── Candidate search per decade ───────────────────────────────────────────────

def _ocr_candidate(r: dict) -> dict:
    """Normalize an OCR record to a candidate dict for scoring."""
    return {
        "first_name":    r.get("first_name"),
        "last_name":     r.get("last_name"),
        "birth_year":    r.get("birth_year"),
        "state":         r.get("state"),
        "county":        r.get("county"),
        "sex":           r.get("sex"),
        "race":          r.get("race"),
        "birthplace":    r.get("birthplace"),
        "household":     r.get("_household"),     # injected if fetched
        "age_at_census": r.get("age"),
        "source_table":  r.get("source_table"),
        "source_id":     r.get("ocr_id"),
        "birth_month":   None,
        "_raw":          r,
    }


def _fs_candidate(r: dict, decade: int) -> dict:
    """Normalize a FamilySearch result to a candidate dict."""
    birth_yr = None
    bdate = r.get("birth_date", "")
    if bdate:
        try:
            birth_yr = int(bdate.lstrip("+").split("-")[0])
        except (ValueError, IndexError):
            pass
    # Extract state/county from residence place string like
    # "Jones, McIntosh, Georgia, United States"
    resi = r.get("resi_place", "") or ""
    resi_parts = [p.strip() for p in resi.split(",")]
    resi_state  = None
    resi_county = None
    for part in reversed(resi_parts):
        if part.lower() in ("united states", "usa", "u.s."):
            continue
        if resi_state is None:
            resi_state = part
        elif resi_county is None:
            resi_county = part
            break
    return {
        "first_name":    r.get("given_name"),
        "last_name":     r.get("surname"),
        "birth_year":    birth_yr,
        "state":         resi_state,
        "county":        resi_county,
        "sex":           None,
        "race":          None,
        "birthplace":    r.get("birth_place"),
        "household":     None,
        "age_at_census": (decade - birth_yr) if birth_yr and decade else None,
        "source_table":  "familysearch",
        "source_id":     r.get("fs_id"),
        "birth_month":   None,
        "_raw":          r,
    }


def search_ancestry_by_surname(conn: sqlite3.Connection, last_name: str,
                               state: str, est_birth_year: int,
                               county: str | None = None,
                               window: int = 20) -> list[dict]:
    """
    Surname + estimated birth year search for when the current person was not yet
    born at the target decade (generational crossover).  Returns OCR candidates
    ranked by birth-year proximity to est_birth_year.
    Tries county-specific first, then broadens to state if < 2 results.
    """
    ocr_hits = search_ocr_by_name(
        conn, last_name, first_name="",
        state=state, county=county,
        birth_year=est_birth_year, window=window,
    )
    if county and len(ocr_hits) < 2:
        # Broaden to full state
        ocr_hits += [r for r in search_ocr_by_name(
            conn, last_name, first_name="",
            state=state, birth_year=est_birth_year, window=window,
        ) if r["ocr_id"] not in {h["ocr_id"] for h in ocr_hits}]
    return [_ocr_candidate(r) for r in ocr_hits]


def score_ancestor(candidate: dict, last_name: str, state: str,
                   est_birth_year: int) -> dict:
    """
    Simplified scoring for generational-crossover candidates.
    No age-progression signal (we don't know the parent's name or exact birth year).
    Returns {score, breakdown}.
    """
    bd: dict[str, int] = {}

    # Surname match (40 pts) — the last name is the primary signal
    k_last = candidate.get("last_name") or ""
    last_sim = _name_sim(last_name, k_last)
    sdx_match = soundex(last_name) == soundex(k_last) if last_name and k_last else False
    bd["surname"] = min(40, round(last_sim * 35 + (5 if sdx_match else 0)))

    # Birth year proximity to estimate (25 pts)
    k_birth = candidate.get("birth_year")
    if k_birth and est_birth_year:
        diff = abs(k_birth - est_birth_year)
        bd["birth_proximity"] = max(0, 25 - diff)  # 1 pt per year off
    else:
        bd["birth_proximity"] = 8

    # Geographic plausibility (25 pts)
    c_state = normalize_str(state)
    k_state = normalize_str(candidate.get("state") or "")
    k_county = normalize_str(candidate.get("county") or "")
    if c_state == k_state:
        bd["geography"] = 25
    else:
        bd["geography"] = 5

    # Race — must be Black (10 pts) for FBA mission
    bd["race"] = 10 if candidate.get("is_black") or \
        (candidate.get("race") or "").upper() in ("B", "BL", "BLACK", "M", "MU") else 0

    return {"score": min(100, sum(bd.values())), "breakdown": bd, "flags": ["generational-jump"]}


def search_decade(conn: sqlite3.Connection, current: dict, target_decade: int,
                  fs_client=None) -> list[dict]:
    """
    Find named candidates for current person in target_decade.

    Dispatch order:
      1. OCR corpus (1870 + any decade we have data for in the target state)
      2. FamilySearch (all decades, when FS_TOKEN available) — filtered by birth
         state when known, to avoid cross-family disambiguation errors
    """
    candidates = []
    seen_ids: set[str] = set()

    birth_year  = current.get("birth_year")
    last_name   = current.get("last_name") or ""
    first_name  = current.get("first_name") or ""
    state       = current.get("state") or ""
    county      = current.get("county") or ""
    # birthplace is where they were BORN — more stable signal than residence state
    birthplace  = current.get("birthplace") or ""
    bp_state    = ""
    if birthplace:
        bp_parts = [p.strip() for p in birthplace.replace(", United States", "").split(",")]
        bp_state = bp_parts[-1].strip() if bp_parts else ""

    # ── OCR corpus — 1870 is primary, but also try for any decade with data ──
    ocr_hits = search_ocr_by_name(
        conn, last_name, first_name,
        state=state, county=county,
        birth_year=birth_year, window=5,
    )
    # If few results, broaden by dropping county
    if county and len(ocr_hits) < 3:
        ocr_hits += [r for r in search_ocr_by_name(
            conn, last_name, first_name,
            state=state, birth_year=birth_year, window=5,
        ) if r["ocr_id"] not in {h["ocr_id"] for h in ocr_hits}]
    # If still few, try birth state (different from residence)
    if bp_state and bp_state.lower() != state.lower() and len(ocr_hits) < 3:
        ocr_hits += [r for r in search_ocr_by_name(
            conn, last_name, first_name,
            state=bp_state, birth_year=birth_year, window=5,
        ) if r["ocr_id"] not in {h["ocr_id"] for h in ocr_hits}]

    for r in ocr_hits:
        if r["ocr_id"] in seen_ids:
            continue
        seen_ids.add(r["ocr_id"])
        r["_household"] = None
        c = _ocr_candidate(r)
        c["search_source"] = "ocr_corpus"
        candidates.append(c)

    # ── FamilySearch ──────────────────────────────────────────────────────────
    if fs_client and getattr(fs_client, "available", False):
        fs_hits = fs_client.search_census_decade(
            first_name, last_name, birth_year, state, target_decade,
            birth_state=bp_state or None,
        )
        for r in fs_hits:
            sid = r.get("fs_id", "")
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            c = _fs_candidate(r, target_decade)
            c["search_source"] = "familysearch"
            candidates.append(c)

    return candidates


# ── FamilySearch client extension ─────────────────────────────────────────────

class WalkFamilySearchClient(FamilySearchClient):
    """
    Extends the base FamilySearch client with census-decade-specific search.
    Uses collection IDs to target the correct census year.
    """

    def search_census_decade(self, given_name: str, surname: str,
                              birth_year: int | None, state: str,
                              decade: int, window: int = 5,
                              birth_state: str | None = None) -> list[dict]:
        """
        Search FS tree for a person at a given census decade.

        birth_state: if provided, adds a birth-place filter to avoid picking up
        same-name families from different states (critical for common surnames).
        """
        if not self.available:
            return []
        try:
            import requests as _req
        except ImportError:
            return []

        params: dict = {"count": 20}
        if given_name:
            params["q.givenName"] = given_name
        if surname:
            params["q.surname"] = surname
        if birth_year:
            params["q.birthLikeDate.from"] = str(birth_year - window)
            params["q.birthLikeDate.to"]   = str(birth_year + window)
        if state:
            params["q.residencePlace"] = f"{state.title()}, United States"
        if birth_state and birth_state.lower() != (state or "").lower():
            params["q.birthPlace"] = f"{birth_state.title()}, United States"

        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        try:
            resp = _req.get(
                f"{self.BASE}/platform/tree/search",
                params=params, headers=headers, timeout=12,
            )
            if resp.status_code == 401:
                self.available = False
                return []
            resp.raise_for_status()
            data = resp.json()
            out: list[dict] = []
            persons_seen: set[str] = set()
            for e in data.get("entries", []):
                content = e.get("content", {}).get("gedcomx", {})
                for p in content.get("persons", []):
                    pid = p.get("id", "")
                    if pid in persons_seen:
                        continue
                    # Skip non-primary persons in the result block
                    asc = p.get("display", {}).get("ascendancyNumber")
                    if asc not in ("1", None):
                        continue
                    persons_seen.add(pid)
                    facts = p.get("facts", [])
                    birth_f = next((f for f in facts
                                    if "/Birth" in f.get("type", "")), {})
                    # Find residence/census fact nearest the target decade
                    resi_facts = [f for f in facts
                                  if "/Residence" in f.get("type", "")
                                  or "/Census"    in f.get("type", "")]
                    best_resi: dict = {}
                    best_dist = 999
                    for rf in resi_facts:
                        try:
                            yr = int(rf.get("date", {}).get("original", "")[:4])
                            dist = abs(yr - decade)
                            if dist < best_dist:
                                best_dist = dist
                                best_resi = rf
                        except (ValueError, TypeError):
                            pass
                    name_parts = (p.get("names") or [{}])[0].get("nameForms", [{}])[0].get("parts", [])
                    given  = next((x["value"] for x in name_parts if "/Given"   in x.get("type", "")), "")
                    family = next((x["value"] for x in name_parts if "/Surname" in x.get("type", "")), "")
                    disp   = p.get("display", {})
                    out.append({
                        "fs_id":       pid,
                        "given_name":  given  or disp.get("name", "").split()[0],
                        "surname":     family or " ".join(disp.get("name", "").split()[1:]),
                        "birth_date":  birth_f.get("date", {}).get("formal", ""),
                        "birth_place": birth_f.get("place", {}).get("original", ""),
                        "resi_place":  best_resi.get("place", {}).get("original", ""),
                        "source":      "FamilySearch",
                        "decade":      decade,
                    })
            return out
        except Exception:
            return []

    def _fetch_person_facts(self, person_id: str) -> dict:
        """
        Fetch the full person record for person_id and return a dict with
        birth_year, birth_place, residence_by_decade, given_name, surname.
        The /platform/tree/ancestry endpoint omits facts; this fills them in.
        """
        try:
            import requests as _req
            resp = _req.get(
                f"{self.BASE}/platform/tree/persons/{person_id}",
                headers={"Authorization": f"Bearer {self.token}",
                         "Accept": "application/json"},
                timeout=10,
            )
            if resp.status_code != 200:
                return {}
            data = resp.json()
        except Exception:
            return {}

        p     = (data.get("persons") or [{}])[0]
        facts = p.get("facts", [])
        birth_f  = next((f for f in facts if "/Birth" in f.get("type", "")), {})
        birth_yr = None
        try:
            birth_yr = int(birth_f.get("date", {}).get("original", "")[:4])
        except (ValueError, TypeError):
            pass

        resi_by_decade: dict[int, str] = {}
        for f in facts:
            if "/Residence" in f.get("type", "") or "/Census" in f.get("type", ""):
                try:
                    yr    = int(f.get("date", {}).get("original", "")[:4])
                    dec   = round(yr / 10) * 10
                    place = f.get("place", {}).get("original", "")
                    if place:
                        resi_by_decade[dec] = place
                except (ValueError, TypeError):
                    pass

        name_parts = (p.get("names") or [{}])[0].get("nameForms", [{}])[0].get("parts", [])
        given  = next((x["value"] for x in name_parts if "/Given"   in x.get("type", "")), "")
        family = next((x["value"] for x in name_parts if "/Surname" in x.get("type", "")), "")
        disp   = p.get("display", {})
        return {
            "given_name":          given  or disp.get("name", "").split()[0],
            "surname":             family or " ".join(disp.get("name", "").split()[1:]),
            "birth_year":          birth_yr,
            "birth_place":         birth_f.get("place", {}).get("original", ""),
            "residence_by_decade": resi_by_decade,
        }

    def get_ancestors(self, person_id: str, generations: int = 4) -> list[dict]:
        """
        Return a list of direct-line ancestors for person_id.
        Fetches the ancestry structure first, then fills in facts (birth,
        residence) for each ancestor via individual person lookups, since
        /platform/tree/ancestry omits the facts array.
        ascendancy_number uses Ahnentafel (1=self, 2=father, 3=mother, …).
        """
        if not self.available:
            return []
        try:
            import requests as _req
        except ImportError:
            return []
        try:
            resp = _req.get(
                f"{self.BASE}/platform/tree/ancestry",
                params={"person": person_id, "generations": generations},
                headers={"Authorization": f"Bearer {self.token}",
                         "Accept": "application/json"},
                timeout=12,
            )
            if resp.status_code == 401:
                self.available = False
                return []
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []

        out = []
        for p in data.get("persons", []):
            asc_num_str = p.get("display", {}).get("ascendancyNumber", "")
            try:
                asc_num = int(asc_num_str)
            except (ValueError, TypeError):
                asc_num = 0

            pid  = p.get("id", "")
            disp = p.get("display", {})

            # The ancestry endpoint returns display-only data without facts.
            # Fetch full person data for each ancestor (skip self).
            if asc_num > 1 and pid:
                facts_data = self._fetch_person_facts(pid)
            else:
                facts_data = {}

            given  = facts_data.get("given_name")  or disp.get("name", "").split()[0]
            family = facts_data.get("surname") or " ".join(disp.get("name", "").split()[1:])
            bplace = facts_data.get("birth_place", "")
            bp_parts = [s.strip() for s in bplace.split(",")]
            bp_state = next((s for s in reversed(bp_parts)
                             if s.lower() not in ("united states", "usa", "")), None)

            out.append({
                "fs_id":               pid,
                "given_name":          given,
                "surname":             family,
                "birth_year":          facts_data.get("birth_year"),
                "birth_place":         bplace,
                "state":               bp_state,
                "ascendancy_number":   asc_num,
                "residence_by_decade": facts_data.get("residence_by_decade", {}),
            })
        return out

    def best_ancestor_for_decade(self, person_id: str, decade: int,
                                  max_gen: int = 4,
                                  surname_filter: str | None = None) -> dict | None:
        """
        Return the Ahnentafel ancestor of person_id whose census record is
        closest to `decade`, prioritising the paternal line (even numbers).
        If surname_filter is given, only consider ancestors sharing that surname
        (prevents maternal-line crossover when the walk follows a specific surname).
        Returns None if the ancestry tree can't be fetched or has no match.
        """
        ancestors = self.get_ancestors(person_id, generations=max_gen)
        best: dict | None = None
        best_dist = 999
        for anc in ancestors:
            if anc["ascendancy_number"] <= 1:   # skip self
                continue
            # Stay on the surname line when requested
            if surname_filter and anc["surname"].lower() != surname_filter.lower():
                continue
            for dec, place in anc["residence_by_decade"].items():
                dist = abs(dec - decade)
                if dist < best_dist or (dist == best_dist and
                        best and anc["ascendancy_number"] < best["ascendancy_number"]):
                    best_dist = dist
                    rp = [s.strip() for s in place.split(",")]
                    resi_state  = next((s for s in reversed(rp)
                                        if s.lower() not in ("united states", "usa", "")), None)
                    resi_county = None
                    seen_state = False
                    for part in reversed(rp):
                        if part.lower() in ("united states", "usa", ""):
                            continue
                        if not seen_state:
                            seen_state = True
                            continue
                        resi_county = part
                        break
                    best = {
                        "fs_id":       anc["fs_id"],
                        "given_name":  anc["given_name"],
                        "surname":     anc["surname"],
                        "birth_year":  anc["birth_year"],
                        "birth_date":  f"+{anc['birth_year']}" if anc["birth_year"] else "",
                        "birth_place": anc["birth_place"],
                        "resi_place":  place,
                        "state":       resi_state,
                        "county":      resi_county,
                        "source":      "FamilySearch",
                        "decade":      decade,
                        "ascendancy_number": anc["ascendancy_number"],
                    }
        return best if best_dist <= 15 else None

    def find_person_in_fs(self, first_name: str, last_name: str,
                           birth_year: int | None,
                           state: str | None = None) -> dict | None:
        """
        Search FamilySearch tree for a person by name + birth year.
        Returns {fs_id, given_name, surname, birth_year, birth_place,
                 residence_by_decade} or None if no strong match.

        When the person is not found directly (common for living people born
        after ~1940 who have no census records), automatically tries to find
        their parent in the FS tree (same surname, born 20-35 years earlier)
        so the ancestry crossover can still chain backward from the parent.
        """
        if not self.available:
            return None
        try:
            import requests as _req
        except ImportError:
            return None

        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

        def _search_fs(params_in: dict) -> dict | None:
            try:
                resp = _req.get(
                    f"{self.BASE}/platform/tree/search",
                    params=params_in, headers=headers, timeout=12,
                )
                if resp.status_code == 401:
                    self.available = False
                    return None
                if resp.status_code != 200:
                    return None
                return resp.json()
            except Exception:
                return None

        def _best_match(data: dict, surname: str,
                        byr: int | None, byr_window: int = 5) -> dict | None:
            for e in data.get("entries", []):
                content = e.get("content", {}).get("gedcomx", {})
                for p in content.get("persons", []):
                    if p.get("display", {}).get("ascendancyNumber") not in ("1", None):
                        continue
                    pid = p.get("id", "")
                    if not pid:
                        continue
                    facts = self._fetch_person_facts(pid)
                    if not facts:
                        continue
                    if _name_sim(facts.get("surname", ""), surname) < 0.7:
                        continue
                    f_birth = facts.get("birth_year")
                    if byr and f_birth and abs(f_birth - byr) > byr_window:
                        continue
                    return {"fs_id": pid, **facts}
            return None

        # ── 1. Direct search: client's own name + birth year ──────────────────
        params: dict = {"count": 5}
        if first_name: params["q.givenName"] = first_name
        if last_name:  params["q.surname"]   = last_name
        if birth_year:
            params["q.birthLikeDate.from"] = str(birth_year - 3)
            params["q.birthLikeDate.to"]   = str(birth_year + 3)
        if state:
            params["q.anyPlace"] = f"{state.title()}, United States"

        data = _search_fs(params)
        if data:
            person = _best_match(data, last_name, birth_year)
            if person and person.get("residence_by_decade"):
                # Verify the residence decades are plausible for this birth year.
                # A person born in 1958 should NOT have a 1900 census residence.
                min_resi = min(person["residence_by_decade"].keys(), default=9999)
                plausible = (birth_year is None) or (min_resi >= birth_year - 5)
                if plausible:
                    return person
                # else: wrong era — fall through to parent search
            elif person and person.get("birth_year") and (
                    birth_year is None or abs(person["birth_year"] - birth_year) <= 5):
                return person

        # ── 2. Parent search: born 20-35 years earlier, same surname ──────────────
        # Used when the client is a living person not in any census (born ~>1940).
        # First tries same first name ("Frank Sr." from "Frank Jr."), then falls
        # back to surname-only to catch different-name parents.
        if birth_year and birth_year > 1935:
            parent_low  = birth_year - 35
            parent_high = birth_year - 18

            # Pass 1: same first name (Sr./Jr. naming pattern)
            p_params: dict = {
                "count": 8,
                "q.surname": last_name,
                "q.birthLikeDate.from": str(parent_low),
                "q.birthLikeDate.to":   str(parent_high),
            }
            if first_name:
                p_params["q.givenName"] = first_name
            if state:
                p_params["q.anyPlace"] = f"{state.title()}, United States"
            p_data = _search_fs(p_params)
            if p_data:
                parent = _best_match(p_data, last_name, None, byr_window=999)
                if parent and parent.get("residence_by_decade"):
                    parent["_is_parent"] = True
                    return parent

            # Pass 2: surname only — handles different-name parents
            if first_name:  # only retry if pass 1 had a first-name filter
                p2_params: dict = {
                    "count": 8,
                    "q.surname": last_name,
                    "q.birthLikeDate.from": str(parent_low),
                    "q.birthLikeDate.to":   str(parent_high),
                }
                if state:
                    p2_params["q.anyPlace"] = f"{state.title()}, United States"
                p2_data = _search_fs(p2_params)
                if p2_data:
                    parent = _best_match(p2_data, last_name, None, byr_window=999)
                    if parent and parent.get("residence_by_decade"):
                        parent["_is_parent"] = True
                        return parent

        return None


# ── Main walk engine ──────────────────────────────────────────────────────────

def run_walk(anchor: dict, conn: sqlite3.Connection,
             fs_client=None) -> dict:
    """
    Walk backward from anchor to 1870.

    anchor = {
        first_name, last_name, birth_year, state, county,
        start_decade (default 1950), verified_by (name of living kin)
    }

    When no _fs_id is supplied and a FamilySearch client is available,
    the engine automatically looks up the anchor in the FS tree so the
    ancestry crossover and residence-cache paths can function without
    the caller knowing any ancestor data in advance.

    Returns chain dict:
    {
        chain_id, anchor, links: [...], chain_product, weakest_link_decade,
        weakest_link_score, status
    }
    """
    # Auto-resolve anchor in FamilySearch if not already known.
    # This is the key step that lets clients supply just name + birth year.
    anchor = dict(anchor)  # don't mutate caller's dict
    if (fs_client and getattr(fs_client, "available", False)
            and not anchor.get("_fs_id")):
        fs_person = fs_client.find_person_in_fs(
            anchor.get("first_name"), anchor.get("last_name"),
            anchor.get("birth_year"), anchor.get("state"),
        )
        if fs_person:
            if fs_person.get("_is_parent"):
                # Client was too young for census corpus; FS returned a parent-generation
                # person (e.g., gave "Frank Jr. b.1958" → found "Frank Sr. b.1933").
                # Store client info as subject metadata; walk starts from the parent.
                anchor["_subject_first"] = anchor.get("first_name")
                anchor["_subject_last"]  = anchor.get("last_name")
                anchor["_subject_birth"] = anchor.get("birth_year")
                # Update anchor identity to the census-era person
                anchor["first_name"] = fs_person.get("given_name") or anchor["first_name"]
                anchor["last_name"]  = fs_person.get("surname")    or anchor["last_name"]
                # Use parent's birth year if known; otherwise leave as None so
                # expected_age defaults to 30 (no false crossover).
                if fs_person.get("birth_year"):
                    anchor["birth_year"] = fs_person["birth_year"]
                else:
                    anchor["birth_year"] = None
            anchor["_fs_id"]         = fs_person["fs_id"]
            anchor["_fs_resi_cache"] = fs_person.get("residence_by_decade", {})

    start_decade = anchor.get("start_decade", 1950)
    if start_decade not in DECADE_LADDER:
        start_decade = min(DECADE_LADDER, key=lambda d: abs(d - start_decade))

    start_idx = DECADE_LADDER.index(start_decade)

    # Anchor is the confirmed starting point — record it as the first link
    chain_links = []
    anchor_link = {
        "decade":          start_decade,
        "first_name":      anchor["first_name"],
        "last_name":       anchor["last_name"],
        "birth_year":      anchor.get("birth_year"),
        "birth_month":     anchor.get("birth_month"),
        "state":           anchor.get("state"),
        "county":          anchor.get("county"),
        "sex":             anchor.get("sex"),
        "race":            anchor.get("race"),
        "age_at_census":   max(0, start_decade - anchor["birth_year"]) if anchor.get("birth_year") else None,
        "source_table":    anchor.get("source_table", "manual"),
        "source_id":       anchor.get("source_id", "anchor"),
        "confidence":      anchor.get("confidence", 100),
        "ipums_validated": False,
        "ipums_histid":    None,
        "link_tier":       "census",
        "runners_up":      [],
        "score_breakdown": {},
        "flags":           [],
        "is_gap":          False,
    }
    chain_links.append(anchor_link)
    current = anchor.copy()

    for i in range(start_idx + 1, len(DECADE_LADDER)):
        target_decade = DECADE_LADDER[i]
        decade_gap    = DECADE_LADDER[i - 1] - target_decade  # 10 normally, 20 for 1900→1880

        current_birth  = current.get("birth_year")
        expected_age   = (target_decade - current_birth) if current_birth else 30

        # ── Generational crossover: person not yet born at target_decade ────────
        if current_birth and expected_age < 0:
            # Estimate how many generations back we need to go
            generations_back   = math.ceil(abs(expected_age) / 30)
            est_ancestor_birth = current_birth - (generations_back * 30)

            # ── Fast path: use the confirmed FS ancestry tree ──────────────────
            # When we have a FamilySearch person ID we trust the tree over OCR,
            # because OCR can match unrelated same-surname children.
            fs_ancestor_used = False
            current_fs_id    = current.get("_fs_id") or current.get("source_id")
            if (fs_client and getattr(fs_client, "available", False)
                    and current_fs_id and current_fs_id != "anchor"):
                anc = fs_client.best_ancestor_for_decade(
                    current_fs_id, target_decade,
                    max_gen=6,  # look deeper for shallow trees
                    surname_filter=current.get("last_name"),
                )
                if anc:
                    cand = _fs_candidate(anc, target_decade)
                    # Confidence: 85 if the tree has a residence fact in this
                    # exact decade, 72 if from a nearby decade (dist ≤ 10).
                    resi_dist = abs(anc.get("decade", target_decade) - target_decade)
                    raw_score = 85 if resi_dist == 0 else 72

                    ipums_val = None
                    if target_decade in (1870, 1880, 1900):
                        check = {
                            "birth_year": cand.get("birth_year") or est_ancestor_birth,
                            "state":  cand.get("state")  or current.get("state"),
                            "county": cand.get("county") or current.get("county"),
                            "sex":    cand.get("sex"),
                        }
                        ipums_val = check_ipums_validation(conn, check, target_decade)
                        if ipums_val:
                            raw_score = min(100, raw_score + ipums_val["bonus"])

                    link = {
                        "decade":          target_decade,
                        "first_name":      cand.get("first_name"),
                        "last_name":       cand.get("last_name") or current["last_name"],
                        "birth_year":      cand.get("birth_year"),
                        "birth_month":     None,
                        "state":           cand.get("state")   or current.get("state"),
                        "county":          cand.get("county")  or current.get("county"),
                        "sex":             cand.get("sex"),
                        "race":            cand.get("race"),
                        "age_at_census":   cand.get("age_at_census"),
                        "source_table":    "familysearch",
                        "source_id":       cand.get("source_id"),
                        "confidence":      raw_score,
                        "ipums_validated": bool(ipums_val),
                        "ipums_histid":    ipums_val["histid"] if ipums_val else None,
                        "link_tier":       "ipums-validated" if ipums_val else "census",
                        "runners_up":      [],
                        "score_breakdown": {},
                        "flags":           ["fs_ancestry_tree", "_generational_jump"],
                        "is_gap":          False,
                    }
                    chain_links.append(link)
                    # Fetch the ancestor's full residence history so subsequent
                    # steps can use their known decade residences directly.
                    anc_fs_id   = cand.get("source_id") or ""
                    anc_decades: dict[int, str] = {}
                    if anc_fs_id:
                        anc_facts = fs_client._fetch_person_facts(anc_fs_id)
                        anc_decades = anc_facts.get("residence_by_decade", {})
                    current = {
                        "first_name": cand.get("first_name"),
                        "last_name":  cand.get("last_name")  or current["last_name"],
                        "birth_year": cand.get("birth_year"),
                        "state":      cand.get("state")      or current.get("state"),
                        "county":     cand.get("county")     or current.get("county"),
                        "sex":        cand.get("sex"),
                        "race":       cand.get("race"),
                        "birthplace": cand.get("birthplace"),
                        "household":  None,
                        "_fs_id":          anc_fs_id or current_fs_id,
                        "_fs_resi_cache":  anc_decades,
                    }
                    fs_ancestor_used = True

            if fs_ancestor_used:
                if target_decade == 1870:
                    break
                continue

            # ── Fallback: OCR surname search + optional FS text search ─────────
            # For ancestor searches, birthplace is a better origin than current
            # residence (e.g., a family that migrated GA after 1900 has SC ancestors).
            bp_raw = (current.get("birthplace") or "").replace(", United States", "").strip()
            ancestry_state = bp_raw.split(",")[-1].strip() if bp_raw else ""
            if not ancestry_state:
                ancestry_state = current.get("state") or ""
            candidates_raw = search_ancestry_by_surname(
                conn, current["last_name"], ancestry_state,
                est_ancestor_birth, county=None,  # broaden to full state
                window=20,
            )
            if fs_client and getattr(fs_client, "available", False):
                fs_hits = fs_client.search_census_decade(
                    "", current["last_name"], est_ancestor_birth,
                    ancestry_state or current.get("state") or "", target_decade,
                    birth_state=ancestry_state or None,
                )
                for r in fs_hits:
                    candidates_raw.append(_fs_candidate(r, target_decade))

            scored = []
            for cand in candidates_raw:
                result = score_ancestor(cand, current["last_name"],
                                        current.get("state") or "",
                                        est_ancestor_birth)
                scored.append({**cand, "_link_score": result["score"],
                               "_breakdown": result["breakdown"],
                               "_flags": result["flags"]})
            scored.sort(key=lambda x: x["_link_score"], reverse=True)

            if not scored or scored[0]["_link_score"] < MIN_LINK_SCORE:
                chain_links.append({
                    "decade": target_decade, "first_name": None,
                    "last_name": current.get("last_name"), "birth_year": None,
                    "birth_month": None, "state": None, "county": None,
                    "sex": None, "race": None, "age_at_census": None,
                    "source_table": "none", "source_id": None,
                    "confidence": 0, "ipums_validated": False, "ipums_histid": None,
                    "link_tier": "gap", "runners_up": [], "score_breakdown": {},
                    "flags": ["not_born_yet", "no_ancestor_found"], "is_gap": True,
                })
            else:
                best  = scored[0]
                runup = [{"name": f"{r.get('first_name','?')} {r.get('last_name','?')}".strip(),
                          "score": r["_link_score"], "source": r.get("source_table",""),
                          "id": r.get("source_id","")} for r in scored[1:3]]

                raw_score = best["_link_score"]
                ipums_val = None
                if target_decade in (1870, 1880, 1900):
                    check_person = {
                        "birth_year": best.get("birth_year") or est_ancestor_birth,
                        "state": best.get("state") or current.get("state"),
                        "county": best.get("county"), "sex": best.get("sex"),
                    }
                    ipums_val = check_ipums_validation(conn, check_person, target_decade)
                    if ipums_val:
                        raw_score = min(100, raw_score + ipums_val["bonus"])

                link = {
                    "decade": target_decade,
                    "first_name": best.get("first_name"),
                    "last_name": best.get("last_name"),
                    "birth_year": best.get("birth_year"),
                    "birth_month": best.get("birth_month"),
                    "state": best.get("state"),
                    "county": best.get("county"),
                    "sex": best.get("sex"),
                    "race": best.get("race"),
                    "age_at_census": best.get("age_at_census"),
                    "source_table": best.get("source_table"),
                    "source_id": best.get("source_id"),
                    "confidence": raw_score,
                    "ipums_validated": bool(ipums_val),
                    "ipums_histid": ipums_val["histid"] if ipums_val else None,
                    "link_tier": "ipums-validated" if ipums_val else "census",
                    "runners_up": runup,
                    "score_breakdown": best.get("_breakdown", {}),
                    "flags": best.get("_flags", []),
                    "is_gap": False,
                    "_generational_jump": True,
                }
                chain_links.append(link)
                current = {
                    "first_name": best.get("first_name"),
                    "last_name":  best.get("last_name")  or current["last_name"],
                    "birth_year": best.get("birth_year"),
                    "state":      best.get("state")      or current.get("state"),
                    "county":     best.get("county")     or current.get("county"),
                    "sex":        best.get("sex"),
                    "race":       best.get("race"),
                    "birthplace": best.get("birthplace"),
                    "household":  best.get("household"),
                }

            if target_decade == 1870:
                break
            continue

        # ── Normal case: person exists (possibly as child) ─────────────────────

        # Fast path: if we came from an FS ancestry crossover and have that
        # person's cached residence data, use it directly when available.
        # When the cache exists but has NO entry for this decade (meaning the
        # person wasn't alive / recorded yet), flip to the ancestry tree for
        # the parent instead.
        resi_cache   = current.get("_fs_resi_cache") or {}
        current_fs_c = current.get("_fs_id", "")
        if resi_cache and current_fs_c and current_fs_c != "anchor":
            if target_decade in resi_cache:
                # Use the cached residence directly — no text search needed.
                place = resi_cache[target_decade]
                rp    = [s.strip() for s in place.split(",")]
                rc_state  = next((s for s in reversed(rp)
                                  if s.lower() not in ("united states", "usa", "")), None)
                rc_county = None
                seen_s = False
                for part in reversed(rp):
                    if part.lower() in ("united states", "usa", ""):
                        continue
                    if not seen_s:
                        seen_s = True; continue
                    rc_county = part; break
                raw_score = 85
                ipums_val = None
                if target_decade in (1870, 1880, 1900):
                    check = {"birth_year": current.get("birth_year"),
                             "state": rc_state, "county": rc_county, "sex": current.get("sex")}
                    ipums_val = check_ipums_validation(conn, check, target_decade)
                    if ipums_val:
                        raw_score = min(100, raw_score + ipums_val["bonus"])
                link = {
                    "decade":          target_decade,
                    "first_name":      current.get("first_name"),
                    "last_name":       current.get("last_name"),
                    "birth_year":      current.get("birth_year"),
                    "birth_month":     None,
                    "state":           rc_state,
                    "county":          rc_county,
                    "sex":             current.get("sex"),
                    "race":            current.get("race"),
                    "age_at_census":   ((target_decade - current["birth_year"])
                                        if current.get("birth_year") else None),
                    "source_table":    "familysearch",
                    "source_id":       current_fs_c,
                    "confidence":      raw_score,
                    "ipums_validated": bool(ipums_val),
                    "ipums_histid":    ipums_val["histid"] if ipums_val else None,
                    "link_tier":       "ipums-validated" if ipums_val else "census",
                    "runners_up":      [],
                    "score_breakdown": {},
                    "flags":           ["fs_resi_cache"],
                    "is_gap":          False,
                }
                chain_links.append(link)
                if target_decade == 1870:
                    break
                continue
            else:
                # No cached entry → person wasn't recorded in this decade.
                # Try the ancestry tree for a parent on the same surname line.
                if fs_client and getattr(fs_client, "available", False):
                    anc2 = fs_client.best_ancestor_for_decade(
                        current_fs_c, target_decade,
                        surname_filter=current.get("last_name"),
                    )
                    if anc2:
                        cand2 = _fs_candidate(anc2, target_decade)
                        rp2   = [s.strip() for s in (anc2.get("resi_place","") or "").split(",")]
                        rs2   = next((s for s in reversed(rp2)
                                      if s.lower() not in ("united states","usa","")), None)
                        rc2   = None; seen2 = False
                        for part in reversed(rp2):
                            if part.lower() in ("united states","usa",""): continue
                            if not seen2: seen2=True; continue
                            rc2=part; break
                        resi_dist2 = abs(anc2.get("decade", target_decade) - target_decade)
                        raw2 = 85 if resi_dist2 == 0 else 72
                        ipums2 = None
                        if target_decade in (1870, 1880, 1900):
                            check2 = {"birth_year": cand2.get("birth_year"),
                                      "state": rs2, "county": rc2, "sex": None}
                            ipums2 = check_ipums_validation(conn, check2, target_decade)
                            if ipums2:
                                raw2 = min(100, raw2 + ipums2["bonus"])
                        link2 = {
                            "decade": target_decade,
                            "first_name": cand2.get("first_name"),
                            "last_name":  cand2.get("last_name") or current["last_name"],
                            "birth_year": cand2.get("birth_year"),
                            "birth_month": None,
                            "state":  rs2 or current.get("state"),
                            "county": rc2 or current.get("county"),
                            "sex": None, "race": None,
                            "age_at_census": cand2.get("age_at_census"),
                            "source_table": "familysearch",
                            "source_id":    cand2.get("source_id"),
                            "confidence":   raw2,
                            "ipums_validated": bool(ipums2),
                            "ipums_histid":    ipums2["histid"] if ipums2 else None,
                            "link_tier": "ipums-validated" if ipums2 else "census",
                            "runners_up": [], "score_breakdown": {},
                            "flags": ["fs_ancestry_parent"], "is_gap": False,
                        }
                        chain_links.append(link2)
                        anc2_facts = fs_client._fetch_person_facts(cand2.get("source_id",""))
                        current = {
                            "first_name": cand2.get("first_name"),
                            "last_name":  cand2.get("last_name") or current["last_name"],
                            "birth_year": cand2.get("birth_year"),
                            "state":  rs2 or current.get("state"),
                            "county": rc2 or current.get("county"),
                            "sex": None, "race": None,
                            "birthplace": cand2.get("birthplace"), "household": None,
                            "_fs_id":         cand2.get("source_id",""),
                            "_fs_resi_cache": anc2_facts.get("residence_by_decade", {}),
                        }
                        if target_decade == 1870:
                            break
                        continue

        candidates = search_decade(conn, current, target_decade, fs_client)

        # Score all candidates
        scored = []
        for cand in candidates:
            result = score_link(current, cand, decade_gap=decade_gap)
            scored.append({**cand, "_link_score": result["score"],
                           "_breakdown": result["breakdown"],
                           "_flags": result["flags"]})
        scored.sort(key=lambda x: x["_link_score"], reverse=True)

        if not scored or scored[0]["_link_score"] < MIN_LINK_SCORE:
            # Gap — no confident match found
            src = "familysearch" if (not scored) else scored[0]["source_table"]
            link = {
                "decade":          target_decade,
                "first_name":      None,
                "last_name":       current.get("last_name"),
                "birth_year":      current.get("birth_year"),
                "birth_month":     None,
                "state":           None,
                "county":          None,
                "sex":             current.get("sex"),
                "race":            current.get("race"),
                "age_at_census":   None,
                "source_table":    src,
                "source_id":       None,
                "confidence":      0,
                "ipums_validated": False,
                "ipums_histid":    None,
                "link_tier":       "gap",
                "runners_up":      [],
                "score_breakdown": {},
                "flags":           ["no_match"],
                "is_gap":          True,
            }
            chain_links.append(link)
            # Don't update current — carry forward last known state
        else:
            best  = scored[0]
            runup = [{"name":  f"{r.get('first_name','')} {r.get('last_name','')}".strip(),
                      "score": r["_link_score"],
                      "source": r.get("source_table", ""),
                      "id":    r.get("source_id", r.get("_raw", {}).get("ocr_id", ""))}
                     for r in scored[1:3]]

            raw_score = best["_link_score"]

            # IPUMS validation bonus
            ipums_val = None
            if target_decade in (1870, 1880, 1900):
                # Use best candidate's birth_year + geography for IPUMS check
                check_person = {
                    "birth_year": best.get("birth_year") or current.get("birth_year"),
                    "state":      best.get("state")      or current.get("state"),
                    "county":     best.get("county")     or current.get("county"),
                    "sex":        best.get("sex")        or current.get("sex"),
                }
                ipums_val = check_ipums_validation(conn, check_person, target_decade)
                if ipums_val:
                    raw_score = min(100, raw_score + ipums_val["bonus"])

            tier = "ipums-validated" if ipums_val else "census"

            link = {
                "decade":          target_decade,
                "first_name":      best.get("first_name"),
                "last_name":       best.get("last_name"),
                "birth_year":      best.get("birth_year"),
                "birth_month":     best.get("birth_month"),
                "state":           best.get("state"),
                "county":          best.get("county"),
                "sex":             best.get("sex"),
                "race":            best.get("race"),
                "age_at_census":   best.get("age_at_census"),
                "source_table":    best.get("source_table"),
                "source_id":       best.get("source_id"),
                "confidence":      raw_score,
                "ipums_validated": bool(ipums_val),
                "ipums_histid":    ipums_val["histid"] if ipums_val else None,
                "link_tier":       tier,
                "runners_up":      runup,
                "score_breakdown": best.get("_breakdown", {}),
                "flags":           best.get("_flags", []),
                "is_gap":          False,
            }
            chain_links.append(link)

            # Update current for next step
            fs_id = (best.get("source_id") or "")
            if best.get("source_table") != "familysearch":
                fs_id = current.get("_fs_id", "")
            current = {
                "first_name": best.get("first_name") or current.get("first_name"),
                "last_name":  best.get("last_name")  or current.get("last_name"),
                "birth_year": best.get("birth_year") or current.get("birth_year"),
                "state":      best.get("state")      or current.get("state"),
                "county":     best.get("county")     or current.get("county"),
                "sex":        best.get("sex")        or current.get("sex"),
                "race":       best.get("race")       or current.get("race"),
                "birthplace": best.get("birthplace") or current.get("birthplace"),
                "household":  best.get("household"),
                "_fs_id":     fs_id,
            }

        if target_decade == 1870:
            break

    # ── Chain product + weakest link ──────────────────────────────────────────
    scored_links = [l for l in chain_links if not l["is_gap"] and l.get("source_id") != "anchor"]
    if scored_links:
        product = 1.0
        for l in scored_links:
            product *= (l["confidence"] / 100.0)
        weakest = min(scored_links, key=lambda x: x["confidence"])
    else:
        product = 0.0
        weakest = None

    chain_id = hashlib.md5(
        f"{anchor['last_name']}:{anchor.get('birth_year')}:{start_decade}:{datetime.datetime.now().isoformat()}".encode()
    ).hexdigest()[:16].upper()

    return {
        "chain_id":            chain_id,
        "anchor":              anchor,
        "links":               chain_links,
        "chain_product":       round(product, 4),
        "weakest_link_decade": weakest["decade"] if weakest else None,
        "weakest_link_score":  weakest["confidence"] if weakest else None,
        "status":              "draft",
        "fs_available":        fs_client is not None and getattr(fs_client, "available", False),
    }


# ── Persist to DB ─────────────────────────────────────────────────────────────

def save_walk(chain: dict, conn: sqlite3.Connection,
              member_id: str | None = None) -> str:
    """Save a walk chain to DB. Returns chain_id."""
    ensure_schema(conn)
    anchor = chain["anchor"]

    # Build notes JSON to store subject (client) metadata + runtime flags
    notes_meta = {}
    for k in ("_subject_first", "_subject_last", "_subject_birth"):
        if anchor.get(k):
            notes_meta[k] = anchor[k]
    if chain.get("fs_available") is not None:
        notes_meta["fs_available"] = bool(chain["fs_available"])
    notes_json = json.dumps(notes_meta) if notes_meta else None

    conn.execute("""
        INSERT INTO walk_chains
          (chain_id, member_id, anchor_first_name, anchor_last_name,
           anchor_birth_year, anchor_state, anchor_county, anchor_start_decade,
           anchor_verified_by, chain_product, weakest_link_decade, weakest_link_score,
           status, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(chain_id) DO UPDATE SET
          chain_product=excluded.chain_product,
          weakest_link_decade=excluded.weakest_link_decade,
          weakest_link_score=excluded.weakest_link_score,
          status=excluded.status,
          notes=excluded.notes
    """, (
        chain["chain_id"], member_id,
        anchor.get("first_name"), anchor.get("last_name"),
        anchor.get("birth_year"), anchor.get("state"), anchor.get("county"),
        anchor.get("start_decade", 1950), anchor.get("verified_by"),
        chain["chain_product"],
        chain["weakest_link_decade"],
        chain["weakest_link_score"],
        chain["status"],
        notes_json,
    ))

    for idx, link in enumerate(chain["links"]):
        lid = hashlib.md5(f"{chain['chain_id']}:{link['decade']}".encode()).hexdigest()[:16].upper()
        conn.execute("""
            INSERT INTO walk_links
              (link_id, chain_id, step_order, decade,
               first_name, last_name, birth_year, birth_month,
               state, county, sex, race, age_at_census,
               source_table, source_id, confidence,
               ipums_validated, ipums_histid, link_tier,
               runners_up, score_breakdown, is_gap)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(link_id) DO UPDATE SET confidence=excluded.confidence
        """, (
            lid, chain["chain_id"], idx, link["decade"],
            link.get("first_name"), link.get("last_name"),
            link.get("birth_year"), link.get("birth_month"),
            link.get("state"), link.get("county"),
            link.get("sex"), link.get("race"),
            link.get("age_at_census"),
            link.get("source_table"), link.get("source_id"),
            link.get("confidence", 0),
            int(link.get("ipums_validated", False)),
            link.get("ipums_histid"),
            link.get("link_tier", "census"),
            json.dumps(link.get("runners_up", [])),
            json.dumps(link.get("score_breakdown", {})),
            int(link.get("is_gap", False)),
        ))

    conn.commit()
    return chain["chain_id"]


def load_walk(chain_id: str, conn: sqlite3.Connection) -> dict | None:
    """Load a walk chain from DB."""
    row = conn.execute("SELECT * FROM walk_chains WHERE chain_id=?", (chain_id,)).fetchone()
    if not row:
        return None
    cols = [d[1] for d in conn.execute("PRAGMA table_info(walk_chains)").fetchall()]
    chain_meta = dict(zip(cols, row))

    link_rows = conn.execute(
        "SELECT * FROM walk_links WHERE chain_id=? ORDER BY step_order",
        (chain_id,)
    ).fetchall()
    link_cols = [d[1] for d in conn.execute("PRAGMA table_info(walk_links)").fetchall()]
    links = []
    for lr in link_rows:
        d = dict(zip(link_cols, lr))
        d["runners_up"]      = json.loads(d.get("runners_up") or "[]")
        d["score_breakdown"] = json.loads(d.get("score_breakdown") or "{}")
        d["ipums_validated"] = bool(d.get("ipums_validated"))
        d["is_gap"]          = bool(d.get("is_gap"))
        links.append(d)

    notes_meta = {}
    raw_notes = chain_meta.get("notes")
    if raw_notes:
        try:
            notes_meta = json.loads(raw_notes)
        except (ValueError, TypeError):
            pass

    anchor_dict = {
        "first_name":    chain_meta["anchor_first_name"],
        "last_name":     chain_meta["anchor_last_name"],
        "birth_year":    chain_meta["anchor_birth_year"],
        "state":         chain_meta["anchor_state"],
        "county":        chain_meta["anchor_county"],
        "start_decade":  chain_meta["anchor_start_decade"],
        "verified_by":   chain_meta["anchor_verified_by"],
    }
    # Restore subject metadata if anchor was auto-shifted to a parent
    for k in ("_subject_first", "_subject_last", "_subject_birth"):
        if notes_meta.get(k):
            anchor_dict[k] = notes_meta[k]

    return {
        "chain_id":            chain_meta["chain_id"],
        "member_id":           chain_meta.get("member_id"),
        "anchor":              anchor_dict,
        "links":               links,
        "chain_product":       chain_meta.get("chain_product", 0),
        "weakest_link_decade": chain_meta.get("weakest_link_decade"),
        "weakest_link_score":  chain_meta.get("weakest_link_score"),
        "status":              chain_meta.get("status", "draft"),
        "created_at":          chain_meta.get("created_at"),
        "fs_available":        notes_meta.get("fs_available", True),
    }


def list_walks(conn: sqlite3.Connection) -> list[dict]:
    """List all walk chains."""
    ensure_schema(conn)
    rows = conn.execute("""
        SELECT chain_id, member_id, anchor_first_name, anchor_last_name,
               anchor_birth_year, anchor_state, anchor_start_decade,
               chain_product, weakest_link_score, status, created_at
        FROM walk_chains ORDER BY created_at DESC
    """).fetchall()
    return [
        {
            "chain_id":        r[0],
            "member_id":       r[1],
            "anchor_name":     f"{r[2] or ''} {r[3] or ''}".strip(),
            "birth_year":      r[4],
            "state":           r[5],
            "start_decade":    r[6],
            "chain_product":   r[7],
            "weakest_score":   r[8],
            "status":          r[9],
            "created_at":      r[10],
        }
        for r in rows
    ]


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_chain(chain: dict) -> None:
    print(f"\n{'='*70}")
    print(f"  ANCHORED BACKWARD WALK — {chain['chain_id']}")
    print(f"  Anchor: {chain['anchor'].get('first_name')} {chain['anchor'].get('last_name')} "
          f"(b.~{chain['anchor'].get('birth_year')}, "
          f"{chain['anchor'].get('county','?')} Co. {chain['anchor'].get('state','?')})")
    print(f"  Chain product: {chain['chain_product']:.0%}   "
          f"Weakest link: decade {chain['weakest_link_decade']} "
          f"@ {chain['weakest_link_score']}/100")
    print(f"{'='*70}")

    for link in chain["links"]:
        decade    = link["decade"]
        is_gap    = link.get("is_gap", False)
        name      = f"{link.get('first_name','?')} {link.get('last_name','?')}".strip()
        county    = link.get("county") or "?"
        state     = link.get("state") or "?"
        conf      = link.get("confidence", 0)
        tier      = link.get("link_tier", "census")
        validated = " ★ipums-validated" if link.get("ipums_validated") else ""
        tier_sym  = "- -" if is_gap else ("════" if tier == "ipums-validated" else "────")

        if is_gap:
            print(f"  {decade}  [GAP — no confident match found]{validated}")
        else:
            src = link.get("source_table", "")
            print(f"  {decade}  {name:<28} {county}, {state:<15}  "
                  f"{conf:>3}/100  {tier}{validated}")
            if link.get("runners_up"):
                for ru in link["runners_up"]:
                    print(f"          runner-up: {ru.get('name','?'):<26} {ru.get('score',0):>3}/100")
            bd = link.get("score_breakdown", {})
            if bd:
                parts = [f"{k}={v}" for k, v in bd.items() if v]
                print(f"          breakdown: {' | '.join(parts)}")
        print(f"  {tier_sym}")

    last_link = chain["links"][-1] if chain["links"] else {}
    if last_link.get("decade") == 1870 and not last_link.get("is_gap"):
        print(f"  → Hand to Bridge Engine: {last_link.get('first_name','')} "
              f"{last_link.get('last_name','')} 1870")
    print()


def _test_bacon(conn: sqlite3.Connection) -> None:
    """Test the engine against the known Bacon lineage."""
    print("\n[TEST] Running Anchored Backward Walk on Bacon lineage...")
    print("  Anchor: Clifton Bacon, b.~1907, McIntosh County GA")
    print("  Expected: walk reaches Simon/Washington Bacon in 1870")
    print()

    anchor = {
        "first_name":    "Clifton",
        "last_name":     "Bacon",
        "birth_year":    1907,
        "state":         "Georgia",
        "county":        "McIntosh",
        "start_decade":  1950,
        "verified_by":   "Test — Bacon lineage hand-verified",
        "confidence":    100,
        "source_table":  "manual",
        "source_id":     "anchor",
    }

    chain = run_walk(anchor, conn)
    _print_chain(chain)

    chain_id = save_walk(chain, conn, member_id="MEMBER-BACON-CLIFTON-1906")
    print(f"  Saved as chain_id: {chain_id}")
    print(f"  View at: http://localhost:5050/walk/{chain_id}")


def main():
    parser = argparse.ArgumentParser(description="ΦΒΑ Anchored Backward Walk Engine")
    parser.add_argument("--first",        help="Anchor first name")
    parser.add_argument("--last",         help="Anchor last name")
    parser.add_argument("--birthyr",      type=int, help="Anchor birth year")
    parser.add_argument("--state",        help="Anchor state")
    parser.add_argument("--county",       help="Anchor county")
    parser.add_argument("--start-decade", type=int, default=1950,
                        help="Census year to anchor in (default 1950)")
    parser.add_argument("--verified-by",  default="CLI test",
                        help="Name of living kin who confirmed anchor")
    parser.add_argument("--member",       help="Attach to member_id")
    parser.add_argument("--test-bacon",   action="store_true",
                        help="Run test against known Bacon lineage")
    parser.add_argument("--show",         help="Print a saved chain by chain_id")
    parser.add_argument("--report",       action="store_true",
                        help="List all saved walk chains")
    args = parser.parse_args()

    conn = open_db()
    ensure_schema(conn)

    if args.report:
        walks = list_walks(conn)
        if not walks:
            print("No walk chains saved yet.")
        else:
            print(f"\n{'Chain ID':<18} {'Anchor':<28} {'Start'} {'Product':>8} {'Weakest':>8}  Status")
            print("-" * 80)
            for w in walks:
                print(f"{w['chain_id']:<18} {w['anchor_name']:<28} {w['start_decade']}  "
                      f"{(w['chain_product'] or 0):.0%}    {w['weakest_score'] or 0:>4}/100  "
                      f"{w['status']}")
        conn.close()
        return

    if args.show:
        chain = load_walk(args.show, conn)
        if chain:
            _print_chain(chain)
        else:
            print(f"Chain not found: {args.show}")
        conn.close()
        return

    if args.test_bacon:
        _test_bacon(conn)
        conn.close()
        return

    if not all([args.first, args.last, args.birthyr, args.state]):
        parser.print_help()
        conn.close()
        return

    anchor = {
        "first_name":   args.first,
        "last_name":    args.last,
        "birth_year":   args.birthyr,
        "state":        args.state,
        "county":       args.county,
        "start_decade": args.start_decade,
        "verified_by":  args.verified_by,
        "confidence":   100,
        "source_table": "manual",
        "source_id":    "anchor",
    }

    fs_client = WalkFamilySearchClient()
    chain = run_walk(anchor, conn, fs_client=fs_client if fs_client.available else None)
    _print_chain(chain)

    chain_id = save_walk(chain, conn, member_id=args.member)
    print(f"Saved as: {chain_id}")
    conn.close()


if __name__ == "__main__":
    main()
