#!/usr/bin/env python3
"""
Phi Beta Alpha Historical Lineage Society
Member Ancestor Lookup — v1.0

Given a member's ancestor name + demographics, queries:
  1. IPUMS linked census database (primary, always available)
  2. FamilySearch API (optional — requires free account + FS_TOKEN env var)

Produces a ranked list of top 5 HISTID candidates with confidence scores.

Usage (CLI):
  python3 member_lookup.py --first Moses --last Freeman --birthyr 1845 \
                            --state Georgia --county Baldwin

Usage (module):
  from member_lookup import run_lookup
  results = run_lookup("Moses", "Freeman", 1845, "Georgia", "Baldwin")

FamilySearch setup (optional, free):
  1. Register at https://www.familysearch.org/developers/
  2. Create an app → get a session token
  3. export FS_TOKEN="your-session-token"
"""

import sqlite3
import os
import sys
import json
import argparse
from unicodedata import normalize

# ── Optional FamilySearch ─────────────────────────────────────────────────────
try:
    import requests
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

# ── Paths ─────────────────────────────────────────────────────────────────────
DB_PATH = os.path.expanduser("~/Documents/phi-beta-alpha/processed/lineage_1870.db")

# ── State FIPS map ────────────────────────────────────────────────────────────
STATE_FIPS = {
    "alabama": 1, "alaska": 2, "arizona": 4, "arkansas": 5, "california": 6,
    "colorado": 8, "connecticut": 9, "delaware": 10, "district of columbia": 11,
    "dc": 11, "florida": 12, "georgia": 13, "hawaii": 15, "idaho": 16,
    "illinois": 17, "indiana": 18, "iowa": 19, "kansas": 20, "kentucky": 21,
    "louisiana": 22, "maine": 23, "maryland": 24, "massachusetts": 25,
    "michigan": 26, "minnesota": 27, "mississippi": 28, "missouri": 29,
    "montana": 30, "nebraska": 31, "nevada": 32, "new hampshire": 33,
    "new jersey": 34, "new mexico": 35, "new york": 36, "north carolina": 37,
    "north dakota": 38, "ohio": 39, "oklahoma": 40, "oregon": 41,
    "pennsylvania": 42, "rhode island": 44, "south carolina": 45,
    "south dakota": 46, "tennessee": 47, "texas": 48, "utah": 49,
    "vermont": 50, "virginia": 51, "washington": 53, "west virginia": 54,
    "wisconsin": 55, "wyoming": 56,
}

# Georgia NHGIS county codes → names
GA_COUNTY_NHGIS = {
    "appling": 1300210, "atkinson": 1300270, "bacon": 1300290, "baker": 1300310,
    "baldwin": 1300330, "banks": 1300350, "barrow": 1300370, "bartow": 1300390,
    "ben hill": 1300430, "berrien": 1300450, "bibb": 1300470, "bleckley": 1300490,
    "brantley": 1300510, "brooks": 1300530, "bryan": 1300550, "bulloch": 1300570,
    "burke": 1300590, "butts": 1300610, "calhoun": 1300630, "camden": 1300650,
    "carroll": 1300690, "catoosa": 1300710, "charlton": 1300730, "chatham": 1300750,
    "chattahoochee": 1300770, "chattooga": 1300790, "cherokee": 1300810,
    "clarke": 1300830, "clay": 1300850, "clayton": 1300870, "clinch": 1300910,
    "cobb": 1300930, "coffee": 1300950, "colquitt": 1300970, "columbia": 1300990,
    "cook": 1301010, "coweta": 1301030, "crawford": 1301050, "crisp": 1301070,
    "dade": 1301090, "dawson": 1301110, "decatur": 1301130, "dekalb": 1301150,
    "dodge": 1301170, "dooly": 1301190, "dougherty": 1301210, "douglas": 1301230,
    "early": 1301250, "echols": 1301270, "effingham": 1301290, "elbert": 1301310,
    "emanuel": 1301330, "evans": 1301350, "fannin": 1301370, "fayette": 1301390,
    "floyd": 1301410, "forsyth": 1301430, "franklin": 1301450, "fulton": 1301490,
    "gilmer": 1301510, "glascock": 1301530, "glynn": 1301550, "gordon": 1301570,
    "grady": 1301590, "greene": 1301610, "gwinnett": 1301630, "habersham": 1301650,
    "hall": 1301670, "hancock": 1301690, "haralson": 1301710, "harris": 1301730,
    "hart": 1301750, "heard": 1301770, "henry": 1301790, "houston": 1301810,
    "irwin": 1301830, "jackson": 1301850, "jasper": 1301870, "jeff davis": 1301890,
    "jefferson": 1301910, "jenkins": 1301930, "johnson": 1301950, "jones": 1301970,
    "lamar": 1301990, "lanier": 1302010, "laurens": 1302030, "lee": 1302050,
    "liberty": 1302070, "lincoln": 1302090, "long": 1302110, "lowndes": 1302130,
    "lumpkin": 1302150, "mcduffie": 1302170, "mcintosh": 1302190, "macon": 1302210,
    "madison": 1302230, "marion": 1302250, "meriwether": 1302270, "miller": 1302290,
    "mitchell": 1302310, "monroe": 1302330, "montgomery": 1302350, "morgan": 1302370,
    "murray": 1302390, "muscogee": 1302410, "newton": 1302430, "oconee": 1302450,
    "oglethorpe": 1302470, "paulding": 1302490, "peach": 1302510, "pickens": 1302530,
    "pierce": 1302550, "pike": 1302570, "polk": 1302590, "pulaski": 1302610,
    "putnam": 1302630, "quitman": 1302650, "rabun": 1302670, "randolph": 1302690,
    "richmond": 1302710, "rockdale": 1302730, "schley": 1302750, "screven": 1302770,
    "seminole": 1302790, "spalding": 1302810, "stephens": 1302830, "stewart": 1302850,
    "sumter": 1302870, "talbot": 1302890, "taliaferro": 1302910, "tattnall": 1302930,
    "taylor": 1302950, "telfair": 1302970, "terrell": 1302990, "thomas": 1303010,
    "tift": 1303030, "toombs": 1303050, "towns": 1303070, "treutlen": 1303090,
    "troup": 1303110, "turner": 1303130, "twiggs": 1303150, "union": 1303170,
    "upson": 1303190, "walker": 1303210, "walton": 1303230, "ware": 1303250,
    "warren": 1303270, "washington": 1303290, "wayne": 1303310, "webster": 1303330,
    "wheeler": 1303350, "white": 1303370, "whitfield": 1303390, "wilcox": 1303410,
    "wilkes": 1303430, "wilkinson": 1303450, "worth": 1303470,
}
NHGIS_TO_NAME = {v: k.title() for k, v in GA_COUNTY_NHGIS.items()}

# Common male/female first names for sex inference (Reconstruction-era focus)
MALE_NAMES = {
    "moses", "james", "john", "william", "george", "henry", "charles", "thomas",
    "robert", "richard", "joseph", "samuel", "david", "daniel", "edward",
    "benjamin", "nathaniel", "peter", "simon", "elijah", "isaiah", "aaron",
    "jacob", "solomon", "caesar", "prince", "jackson", "washington", "lincoln",
    "andrew", "frank", "alfred", "albert", "cornelius", "lewis", "eli", "adam",
    "noah", "isaac", "tobias", "ezekiel", "jeremiah", "caleb", "josiah",
}
FEMALE_NAMES = {
    "mary", "sarah", "elizabeth", "martha", "jane", "emily", "ann", "anna",
    "hannah", "lucy", "rachel", "rebecca", "susanna", "ruth", "esther",
    "catherine", "margaret", "ellen", "eliza", "harriet", "charlotte",
    "julia", "caroline", "virginia", "louisa", "matilda", "frances",
    "clarissa", "diana", "chloe", "phillis", "venus", "minerva", "juno",
    "rosa", "rose", "georgia", "america", "columbia", "temperance",
}


# ════════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def normalize_str(s):
    return normalize("NFKD", s.lower().strip())


def infer_sex(first_name):
    """Returns 1 (male), 2 (female), or 0 (unknown)."""
    n = normalize_str(first_name)
    if n in MALE_NAMES:
        return 1
    if n in FEMALE_NAMES:
        return 2
    return 0


def soundex(name):
    """Classic Soundex algorithm for phonetic matching."""
    name = name.upper().strip()
    if not name:
        return "0000"
    code_map = {
        "BFPV": "1", "CGJKQSXYZ": "2", "DT": "3",
        "L": "4", "MN": "5", "R": "6",
    }
    def char_code(c):
        for keys, val in code_map.items():
            if c in keys:
                return val
        return "0"

    first = name[0]
    result = first
    prev = char_code(first)
    for ch in name[1:]:
        c = char_code(ch)
        if c != "0" and c != prev:
            result += c
        prev = c if c != "0" else prev
    result = result.ljust(4, "0")[:4]
    return result


def county_nhgis(state_fips, county_name):
    """Return NHGIS county code for Georgia. Returns None for other states."""
    if state_fips != 13 or not county_name:
        return None
    return GA_COUNTY_NHGIS.get(normalize_str(county_name))


def county_display(nhgis_code):
    if nhgis_code is None:
        return "Unknown County"
    return NHGIS_TO_NAME.get(int(nhgis_code), f"NHGIS-{nhgis_code}")


# ════════════════════════════════════════════════════════════════════════════════
# STEP 1 — IPUMS DATABASE QUERY
# ════════════════════════════════════════════════════════════════════════════════

def query_ipums(conn, statefip, birthyr, birth_window=5,
                county_nhgis_code=None, sex_hint=0):
    """
    Returns all demographic candidates from the IPUMS census DB.
    Searches Tier-3 first (all 3 censuses confirmed), falls back to Tier-2/1.
    """
    bmin = birthyr - birth_window
    bmax = birthyr + birth_window

    results = []
    for tier, link_clause in [
        (3, "LINK1880 != 0 AND LINK1900 != 0"),
        (2, "(LINK1880 != 0 OR LINK1900 != 0) AND NOT (LINK1880 != 0 AND LINK1900 != 0)"),
        (1, "LINK1880 = 0 AND LINK1900 = 0"),
    ]:
        conditions = [
            "STATEFIP = :statefip",
            "BIRTHYR BETWEEN :bmin AND :bmax",
            link_clause,
        ]
        params = {"statefip": statefip, "bmin": bmin, "bmax": bmax}

        if county_nhgis_code:
            conditions.append("COUNTYNHG = :county")
            params["county"] = county_nhgis_code

        if sex_hint in (1, 2):
            conditions.append("SEX = :sex")
            params["sex"] = sex_hint

        sql = f"""
            SELECT HISTID, YEAR, AGE, BIRTHYR, SEX, COUNTYNHG,
                   LINK1870, LINK1880, LINK1900, BPL
            FROM census
            WHERE {' AND '.join(conditions)}
            ORDER BY ABS(BIRTHYR - :birthyr), YEAR
        """
        params["birthyr"] = birthyr

        rows = conn.execute(sql, params).fetchall()
        for r in rows:
            results.append({
                "histid":      r[0],
                "year":        r[1],
                "age":         r[2],
                "birthyr":     r[3],
                "sex":         "M" if r[4] == 1 else "F",
                "sex_int":     r[4],
                "county_code": r[5],
                "county_name": county_display(r[5]),
                "link1870":    r[6],
                "link1880":    r[7],
                "link1900":    r[8],
                "bpl":         r[9],
                "tier":        tier,
                "source":      "IPUMS",
            })

    return results


# ════════════════════════════════════════════════════════════════════════════════
# STEP 2 — FAMILYSEARCH API (optional)
# ════════════════════════════════════════════════════════════════════════════════

class FamilySearchClient:
    """
    Wraps FamilySearch platform API.
    Requires a session token in the FS_TOKEN environment variable.

    To obtain a token (free):
      1. Register at https://www.familysearch.org/developers/
      2. Create an app, note your client_id
      3. Complete OAuth2 flow to get an access_token
      4. export FS_TOKEN="your-access-token"
    """

    BASE = "https://api.familysearch.org"

    def __init__(self):
        self.token = os.environ.get("FS_TOKEN", "").strip()
        self.available = bool(self.token) and REQUESTS_OK

    def search_records(self, given_name, surname, birthyr, state_name,
                       birth_window=5):
        """
        Search FamilySearch historical records for name + demographics.
        Returns list of dicts with name, birth info, record source.
        """
        if not self.available:
            return []

        params = {
            "q.givenName":    given_name,
            "q.surname":      surname,
            "q.birthLikeYear":    str(birthyr),
            "q.birthLikePlace":   f"{state_name}, United States",
            "count":          20,
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept":        "application/json",
        }
        try:
            resp = requests.get(
                f"{self.BASE}/platform/records/search",
                params=params, headers=headers, timeout=12
            )
            if resp.status_code == 401:
                print("  [FamilySearch] Token expired or invalid — skipping FS lookup.")
                return []
            resp.raise_for_status()
            data = resp.json()
            entries = data.get("entries", [])
            candidates = []
            for e in entries:
                content = e.get("content", {}).get("gedcomx", {})
                persons = content.get("persons", [{}])
                p = persons[0] if persons else {}
                facts = p.get("facts", [])
                birth_fact = next((f for f in facts if f.get("type", "").endswith("/Birth")), {})
                birth_date = birth_fact.get("date", {}).get("formal", "")
                birth_place = birth_fact.get("place", {}).get("original", "")
                names_raw = p.get("names", [{}])
                name_parts = names_raw[0].get("nameForms", [{}])[0].get("parts", []) if names_raw else []
                given  = next((x["value"] for x in name_parts if x.get("type","").endswith("/Given")), "")
                family = next((x["value"] for x in name_parts if x.get("type","").endswith("/Surname")), "")
                candidates.append({
                    "fs_id":       e.get("id", ""),
                    "given_name":  given,
                    "surname":     family,
                    "birth_date":  birth_date,
                    "birth_place": birth_place,
                    "source":      "FamilySearch",
                })
            return candidates
        except Exception as ex:
            print(f"  [FamilySearch] Request error: {ex}")
            return []


# ════════════════════════════════════════════════════════════════════════════════
# STEP 3 — SCORING ENGINE
# ════════════════════════════════════════════════════════════════════════════════

def score_candidate(candidate, query_birthyr, query_sex, query_county_code,
                    fs_candidates, first_name, last_name):
    """
    Scores a single IPUMS candidate 0-100.

    Component breakdown:
      Birth year proximity  : 0-30 pts
      County match          : 0-20 pts
      Verification tier     : 0-25 pts
      Sex consistency       : 0-10 pts
      FamilySearch name hit : 0-15 pts  (if FS available)
      Demographic rarity    : 0-  (implicit — tighter matches score higher)
    """
    score = 0
    factors = []

    # ── Birth year proximity (0-30) ───────────────────────────────────────────
    yr_diff = abs(candidate["birthyr"] - query_birthyr)
    yr_pts = max(0, 30 - (yr_diff * 6))   # 30, 24, 18, 12, 6, 0 for diff 0-5
    score += yr_pts
    factors.append(f"birthyr±{yr_diff}={yr_pts}pts")

    # ── County match (0-20) ───────────────────────────────────────────────────
    if query_county_code and candidate["county_code"]:
        if int(candidate["county_code"]) == query_county_code:
            score += 20
            factors.append("county=exact(20pts)")
        else:
            factors.append("county=miss(0pts)")
    elif not query_county_code:
        # No county filter provided — partial credit for having any county data
        score += 8
        factors.append("county=unfiltered(8pts)")
    else:
        # County code provided but candidate has NULL county
        score += 4
        factors.append("county=null(4pts)")

    # ── Verification tier (0-25) ──────────────────────────────────────────────
    tier_pts = {3: 25, 2: 15, 1: 5}.get(candidate["tier"], 0)
    score += tier_pts
    factors.append(f"tier{candidate['tier']}={tier_pts}pts")

    # ── Sex consistency (0-10) ────────────────────────────────────────────────
    if query_sex in (1, 2):
        if candidate["sex_int"] == query_sex:
            score += 10
            factors.append("sex=match(10pts)")
        else:
            factors.append("sex=mismatch(0pts)")
    else:
        score += 5  # unknown — partial credit
        factors.append("sex=unknown(5pts)")

    # ── FamilySearch name hit (0-15) ──────────────────────────────────────────
    fs_pts = 0
    if fs_candidates:
        sdx_first = soundex(first_name)
        sdx_last  = soundex(last_name)
        for fs in fs_candidates:
            fs_sdx_first = soundex(fs.get("given_name", ""))
            fs_sdx_last  = soundex(fs.get("surname", ""))
            first_match  = (sdx_first == fs_sdx_first) or \
                           (normalize_str(first_name) in normalize_str(fs.get("given_name", "")))
            last_match   = (sdx_last == fs_sdx_last) or \
                           (normalize_str(last_name) in normalize_str(fs.get("surname", "")))
            if first_match and last_match:
                fs_pts = 15
                break
            elif last_match:
                fs_pts = max(fs_pts, 8)
            elif first_match:
                fs_pts = max(fs_pts, 5)
        score += fs_pts
        factors.append(f"fs_name={fs_pts}pts")
    # If no FS available, name score is 0 — overall max becomes 85, rescaled below

    candidate["_score_raw"]   = score
    candidate["_factors"]     = " | ".join(factors)
    candidate["_fs_available"] = bool(fs_candidates is not None)

    return score


def normalize_scores(candidates, fs_available):
    """
    Rescale scores to 0-100 based on whether FamilySearch contributed.
    Without FS, max possible score is 85 (30+20+25+10 = 85).
    With FS, max is 100 (85+15).
    """
    max_raw = 100 if fs_available else 85
    for c in candidates:
        c["confidence"] = round(min(100, c["_score_raw"] / max_raw * 100))
    return candidates


# ════════════════════════════════════════════════════════════════════════════════
# MAIN LOOKUP
# ════════════════════════════════════════════════════════════════════════════════

def run_lookup(first_name, last_name, birthyr, state, county=None,
               birth_window=5, top_n=5, verbose=True):
    """
    Primary entry point for member ancestor lookup.

    Returns list of top_n candidate dicts, each with:
      histid, confidence, tier, census_years, county_name, birthyr,
      sex, source, factors (score breakdown)
    """
    if verbose:
        print("=" * 64)
        print("PHI BETA ALPHA — ANCESTOR LINEAGE LOOKUP")
        print("=" * 64)
        print(f"  Query:   {first_name} {last_name}")
        print(f"  Born:    ~{birthyr} (±{birth_window} years)")
        print(f"  State:   {state}")
        print(f"  County:  {county or 'any'}")
        print()

    # Resolve state FIPS
    statefip = STATE_FIPS.get(normalize_str(state))
    if statefip is None:
        raise ValueError(f"Unknown state: '{state}'. Use full state name.")

    # Resolve county code
    county_code = county_nhgis(statefip, county) if county else None
    if county and statefip == 13 and county_code is None:
        print(f"  WARNING: County '{county}' not found in Georgia NHGIS map — "
              f"searching all counties.")

    # Infer sex from first name
    sex_hint = infer_sex(first_name)
    sex_label = {1: "Male (inferred)", 2: "Female (inferred)", 0: "Unknown"}[sex_hint]

    if verbose:
        print(f"  Sex hint:    {sex_label}")
        print(f"  State FIPS:  {statefip}")
        if county_code:
            print(f"  County NHGIS: {county_code} ({county.title()})")
        print()

    # ── IPUMS query ───────────────────────────────────────────────────────────
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if verbose:
        print(f"[1/3] Querying IPUMS database...")
    ipums_candidates = query_ipums(conn, statefip, birthyr, birth_window,
                                   county_code, sex_hint)
    conn.close()

    tier_counts = {3: 0, 2: 0, 1: 0}
    for c in ipums_candidates:
        tier_counts[c["tier"]] += 1

    if verbose:
        print(f"       Found {len(ipums_candidates):,} IPUMS demographic candidates")
        print(f"       Tier-3 (3-census verified): {tier_counts[3]:,}")
        print(f"       Tier-2 (2-census):          {tier_counts[2]:,}")
        print(f"       Tier-1 (1-census):          {tier_counts[1]:,}")
        print()

    # ── FamilySearch query ────────────────────────────────────────────────────
    fs_client = FamilySearchClient()
    fs_candidates = None

    if verbose:
        print(f"[2/3] Querying FamilySearch...")

    if not REQUESTS_OK:
        if verbose:
            print("       requests library not installed — skipping FamilySearch.")
    elif not fs_client.available:
        if verbose:
            print("       FS_TOKEN not set — FamilySearch skipped.")
            print("       (Set FS_TOKEN env var with a valid session token to enable.)")
        fs_candidates = []
    else:
        fs_candidates = fs_client.search_records(
            first_name, last_name, birthyr, state, birth_window
        )
        if verbose:
            print(f"       Found {len(fs_candidates)} FamilySearch record matches.")
    if verbose:
        print()

    # ── Scoring ───────────────────────────────────────────────────────────────
    if verbose:
        print(f"[3/3] Scoring and ranking candidates...")

    for c in ipums_candidates:
        score_candidate(c, birthyr, sex_hint, county_code,
                        fs_candidates, first_name, last_name)

    ipums_candidates.sort(key=lambda x: x["_score_raw"], reverse=True)
    fs_available = bool(fs_candidates)
    normalize_scores(ipums_candidates, fs_available)

    top = ipums_candidates[:top_n]

    if verbose:
        print(f"       Scored {len(ipums_candidates):,} candidates.")
        print()

    # ── Results display ───────────────────────────────────────────────────────
    if verbose:
        print("=" * 64)
        print(f"TOP {top_n} CANDIDATES  (confidence 0-100)")
        print(f"Data sources: IPUMS{' + FamilySearch' if fs_available else ' only'}")
        print(f"{'Note: FamilySearch unavailable — scores capped at 85 raw, rescaled to 100' if not fs_available else ''}")
        print("=" * 64)
        print()

        for rank, c in enumerate(top, 1):
            census_years = []
            if c["link1870"]: census_years.append("1870")
            if c["link1880"]: census_years.append("1880")
            if c["link1900"]: census_years.append("1900")

            tier_label = {3: "Tier-3 ✓✓✓", 2: "Tier-2 ✓✓", 1: "Tier-1 ✓"}.get(c["tier"], "?")

            print(f"  Rank #{rank}  Confidence: {c['confidence']:>3}/100  [{tier_label}]")
            print(f"  HISTID:   {c['histid']}")
            print(f"  Born:     {c['birthyr']}  Sex: {c['sex']}  "
                  f"Census year: {c['year']}")
            print(f"  County:   {c['county_name']}  BPL: {c['bpl']}")
            print(f"  Census confirmed: {', '.join(census_years) if census_years else 'none'}")
            print(f"  Score breakdown: {c['_factors']}")
            print()

        if not top:
            print("  No candidates found. Try widening birth_window or removing county filter.")
            print()

    # Return structured results
    return [
        {
            "rank":            i + 1,
            "histid":          c["histid"],
            "confidence":      c["confidence"],
            "tier":            c["tier"],
            "census_year":     c["year"],
            "birthyr":         c["birthyr"],
            "sex":             c["sex"],
            "county_name":     c["county_name"],
            "county_code":     c["county_code"],
            "link1870":        c["link1870"],
            "link1880":        c["link1880"],
            "link1900":        c["link1900"],
            "bpl":             c["bpl"],
            "score_breakdown": c["_factors"],
            "source":          c["source"],
        }
        for i, c in enumerate(top)
    ]


# ════════════════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Phi Beta Alpha Ancestor Lineage Lookup"
    )
    parser.add_argument("--first",   required=True, help="Ancestor first name")
    parser.add_argument("--last",    required=True, help="Ancestor last name")
    parser.add_argument("--birthyr", required=True, type=int, help="Estimated birth year")
    parser.add_argument("--state",   required=True, help="State (full name)")
    parser.add_argument("--county",  default=None,  help="County name (optional)")
    parser.add_argument("--window",  default=5, type=int,
                        help="Birth year search window ±N (default 5)")
    parser.add_argument("--top",     default=5, type=int,
                        help="Number of top results to show (default 5)")
    parser.add_argument("--json",    action="store_true",
                        help="Output results as JSON")
    args = parser.parse_args()

    results = run_lookup(
        first_name=args.first,
        last_name=args.last,
        birthyr=args.birthyr,
        state=args.state,
        county=args.county,
        birth_window=args.window,
        top_n=args.top,
        verbose=not args.json,
    )

    if args.json:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
