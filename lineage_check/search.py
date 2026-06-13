"""
Lineage Check search engine — multi-state edition.
Phase 1: name match against ocr_records + census_ocr_georgia_1870 + census_ocr_1870 + bureau_patients
         Layered: exact → FTS5 → Soundex → edit-distance fallback → nickname expansion
Phase 2: IPUMS demographic verification → tier + composite confidence score
"""

import re
import sqlite3
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from member_lookup import soundex, normalize_str, STATE_FIPS, GA_COUNTY_NHGIS, \
    NHGIS_TO_NAME, infer_sex, county_nhgis, county_display, query_ipums, \
    score_candidate, normalize_scores

DB_PATH = Path.home() / "Documents/phi-beta-alpha/processed/lineage_1870.db"

BPL_LABELS = {
    13: "Georgia", 45: "South Carolina", 37: "North Carolina", 51: "Virginia",
    1: "Alabama", 12: "Florida", 28: "Mississippi", 47: "Tennessee",
    22: "Louisiana", 21: "Kentucky", 17: "Illinois", 36: "New York",
    42: "Pennsylvania", 24: "Maryland", 25: "Massachusetts",
}

TARGET_STATES = [
    "All States",
    "Georgia", "Alabama", "Florida", "South Carolina", "North Carolina",
    "Mississippi", "Kentucky", "Louisiana", "Virginia", "New York",
]

# ── Nickname / diminutive table (19th-century Black American names) ────────────
# Maps short form → canonical long form AND long form → short forms for lookup
_NICKNAMES: dict[str, list[str]] = {
    # Male
    "wash": ["washington"], "wash.": ["washington"],
    "si": ["simon", "silas"], "si.": ["simon", "silas"],
    "ben": ["benjamin"], "benny": ["benjamin"],
    "will": ["william"], "willy": ["william"], "bill": ["william"],
    "tom": ["thomas"], "tommy": ["thomas"],
    "sam": ["samuel"], "sammy": ["samuel"],
    "nat": ["nathaniel", "nathan"],
    "jim": ["james"], "jimmy": ["james"],
    "joe": ["joseph"], "joey": ["joseph"],
    "ned": ["edward", "edmund"],
    "fred": ["frederick"],
    "frank": ["francis", "franklin"],
    "abe": ["abraham"],
    "andy": ["andrew"],
    "jake": ["jacob"],
    "dan": ["daniel"],
    "mose": ["moses"], "mo": ["moses"],
    "ike": ["isaac"],
    "tony": ["anthony"],
    "dave": ["david"],
    "charlie": ["charles"], "charley": ["charles"],
    "alex": ["alexander"],
    "henry": ["henrietta"],  # m/f crossover common in records
    "jack": ["jackson", "john"],
    "dick": ["richard"],
    "bob": ["robert"],
    "rob": ["robert"],
    "geo": ["george"],
    "lew": ["lewis", "louis"],
    "jeff": ["jefferson"],
    "eli": ["elijah", "elias"],
    "sol": ["solomon"],
    "lem": ["lemuel"],
    "harry": ["henry", "harold"],
    "pete": ["peter"],
    "pat": ["patrick"],
    "mat": ["matthew", "mathew"],
    # Female
    "patsy": ["martha", "patricia"],
    "polly": ["mary", "molly"],
    "nelly": ["eleanor", "ellen", "helen"],
    "nell": ["eleanor", "ellen", "helen"],
    "betty": ["elizabeth"], "bette": ["elizabeth"],
    "betsy": ["elizabeth"],
    "liz": ["elizabeth"], "lizzie": ["elizabeth"],
    "eliza": ["elizabeth"],
    "susie": ["susan", "susannah"],
    "sue": ["susan", "susannah"],
    "fanny": ["frances", "fanny"],
    "fannie": ["frances"],
    "millie": ["mildred", "amelia"],
    "minnie": ["minerva", "wilhelmina"],
    "hattie": ["harriet", "harriett"],
    "harriet": ["harriett"],
    "sallie": ["sarah"], "sally": ["sarah"],
    "jennie": ["jane", "jennifer"], "jenny": ["jane"],
    "lucy": ["lucinda", "lucia"],
    "lucy ann": ["lucinda"],
    "celia": ["cecelia", "cecilia"],
    "tildy": ["matilda"], "tilda": ["matilda"],
    "milly": ["mildred", "amelia", "millicent"],
    "viney": ["lavinia"],
    "viney ann": ["lavinia"],
    "nance": ["nancy"], "nan": ["nancy"],
    "lottie": ["charlotte"],
    "carrie": ["caroline", "carolina"],
    "cate": ["catherine"], "kate": ["catherine"],
    "kitty": ["catherine"],
    "delia": ["adelia", "cordelia"],
    "dink": ["dinah"],
    "bina": ["sabina", "albina"],
    "rena": ["irena", "lorena", "serena"],
    "tempy": ["temperance"],
    "louisa": ["eliza", "luisa"],
    "lou": ["louisa", "luisa"],
    "mag": ["margaret"], "maggie": ["margaret"],
    "peggy": ["margaret"],
    "annie": ["anna", "anne"],
    "bertie": ["bertha", "alberta"],
    "gussie": ["augusta"],
    # Freedpeople-specific names and forms
    "cinda": ["lucinda"],
    "sina": ["lucinda"],
    "sina ann": ["lucinda"],
    "clarissa": ["clara"],
    "silvy": ["sylvia"],
    "rina": ["marina", "caterina"],
    "sucky": ["susan"],
    "peg": ["peggy", "margaret"],
    "phillis": ["phyllis"],
    "chloe": ["chlora"],
}

# Build reverse map: long form → all known short forms
_NICKNAME_REVERSE: dict[str, list[str]] = {}
for short, longs in _NICKNAMES.items():
    for lg in longs:
        _NICKNAME_REVERSE.setdefault(lg, []).append(short)


def _nickname_variants(name: str) -> list[str]:
    """Return all known nickname variants (both directions) for a given name."""
    if not name:
        return []
    n = name.lower().strip()
    variants = set()
    # short → long
    if n in _NICKNAMES:
        variants.update(_NICKNAMES[n])
    # long → short
    if n in _NICKNAME_REVERSE:
        variants.update(_NICKNAME_REVERSE[n])
    variants.discard(n)
    return list(variants)


# ── Metaphone implementation (single-coded) ────────────────────────────────────
def _metaphone(word: str) -> str:
    word = word.upper().strip()
    if not word:
        return ""
    # Drop trailing S/ED/ING
    for suffix in ("ING", "ED"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            word = word[: -len(suffix)]
    # Initial special cases
    for pair, rep in [("AE", "E"), ("GN", "N"), ("KN", "N"), ("PN", "N"), ("WR", "R")]:
        if word.startswith(pair):
            word = rep + word[2:]
    # Drop trailing E
    if word.endswith("E") and len(word) > 1:
        word = word[:-1]
    code = ""
    vowels = set("AEIOU")
    prev = ""
    for i, ch in enumerate(word):
        if ch in vowels:
            if i == 0:
                code += ch
            prev = ch
            continue
        if ch == prev:
            prev = ch
            continue
        if ch == "B":
            if not (i == len(word) - 1 and prev == "M"):
                code += "B"
        elif ch == "C":
            if i + 1 < len(word) and word[i + 1] in "EIY":
                code += "S"
            elif word[i : i + 2] == "CH":
                code += "X"
            else:
                code += "K"
        elif ch == "D":
            if word[i : i + 2] == "DG" and i + 2 < len(word) and word[i + 2] in "EIY":
                code += "J"
            else:
                code += "T"
        elif ch == "F":
            code += "F"
        elif ch == "G":
            if i + 1 < len(word) and word[i + 1] in "EIY":
                code += "J"
            elif word[i : i + 2] not in ("GH", "GN", "GNE"):
                code += "K"
        elif ch == "H":
            if i + 1 < len(word) and word[i + 1] in vowels and (i == 0 or prev not in vowels):
                code += "H"
        elif ch in "JY":
            code += "Y"
        elif ch == "K":
            if prev != "C":
                code += "K"
        elif ch == "L":
            code += "L"
        elif ch == "M":
            code += "M"
        elif ch == "N":
            code += "N"
        elif ch == "P":
            code += "F" if i + 1 < len(word) and word[i + 1] == "H" else "P"
        elif ch == "Q":
            code += "K"
        elif ch == "R":
            code += "R"
        elif ch == "S":
            if word[i : i + 2] == "SH" or word[i : i + 3] in ("SIO", "SIA"):
                code += "X"
            else:
                code += "S"
        elif ch == "T":
            if word[i : i + 2] == "TH":
                code += "0"
            elif word[i : i + 3] not in ("TIA", "TIO"):
                code += "T"
        elif ch == "V":
            code += "F"
        elif ch == "W":
            if i + 1 < len(word) and word[i + 1] in vowels:
                code += "W"
        elif ch == "X":
            code += "KS"
        elif ch == "Y":
            if i + 1 < len(word) and word[i + 1] in vowels:
                code += "Y"
        elif ch == "Z":
            code += "S"
        prev = ch
    return code[:6]  # cap at 6 chars


# ── Name utilities ─────────────────────────────────────────────────────────────
_ABBREV = {
    "wm": "william", "jas": "james", "thos": "thomas", "chas": "charles",
    "geo": "george", "robt": "robert", "jno": "john", "richd": "richard",
    "edwd": "edward", "saml": "samuel", "beni": "benjamin", "benj": "benjamin",
    "alex": "alexander", "jos": "joseph", "nathl": "nathaniel",
    "danl": "daniel", "eliz": "elizabeth", "marg": "margaret",
    "margt": "margaret", "cath": "catherine", "jeph": "jephtha",
    "benja": "benjamin", "dani": "daniel", "robt": "robert",
    "michl": "michael", "wm.": "william", "jas.": "james",
}

def _expand_name(name):
    """Expand common historical census abbreviations before Soundex matching."""
    if not name:
        return name
    return _ABBREV.get(name.lower().rstrip("."), name)


def _levenshtein(a, b):
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j+1]+1, curr[j]+1, prev[j]+(0 if ca==cb else 1)))
        prev = curr
    return prev[-1]


def _name_similarity(found, query):
    """Normalized similarity 0.0–1.0 between a found name and the search query."""
    if not found and not query:
        return 0.6   # neither side has a name — neutral
    if not found or not query:
        return 0.4   # one side missing — penalize
    a, b = found.lower(), query.lower()
    if a == b:
        return 1.0
    longer = max(len(a), len(b))
    return max(0.0, 1.0 - _levenshtein(a, b) / longer)


def open_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.create_function("soundex_py", 1, soundex)
    conn.create_function("metaphone_py", 1, _metaphone)
    return conn


def _fts_escape(term: str) -> str:
    """Escape a name for FTS5 MATCH query."""
    return '"' + term.replace('"', '""') + '"'


def search_fts(conn, last_name: str, first_name: str = "", state: str = None,
               limit: int = 30) -> list[dict]:
    """
    Fast FTS5 full-text search returning rowid references.
    Returns list of {fts_rowid, source_table, record_id, state, county, score}.
    Uses prefix match so 'Bacn' catches 'Bacon'.
    """
    if not last_name:
        return []
    # Try exact last name first; fall back to prefix
    last_esc = _fts_escape(last_name)
    fts_query = f"last_name:{last_esc}"
    if first_name:
        fts_query += f" first_name:{_fts_escape(first_name)}"

    state_filter = ""
    state_params: list = []
    state_norm = normalize_str(state) if state else None
    if state_norm and state_norm != "all states":
        state_filter = "AND LOWER(m.state) = ?"
        state_params = [state_norm]

    sql = f"""
        SELECT f.rowid as fts_rowid, m.source_table, m.record_id, m.state, m.county,
               rank as fts_rank
        FROM names_fts f
        JOIN fts_rowid_map m ON f.rowid = m.fts_rowid
        WHERE names_fts MATCH ?
        {state_filter}
        ORDER BY rank
        LIMIT ?
    """
    try:
        rows = conn.execute(sql, [fts_query] + state_params + [limit]).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


def get_states_with_ocr(conn):
    """Return set of states that have records in census_ocr_1870."""
    rows = conn.execute(
        "SELECT DISTINCT state FROM census_ocr_1870 WHERE last_name IS NOT NULL"
    ).fetchall()
    states = {r[0] for r in rows}
    states.add("Georgia")  # always included (census_ocr_georgia_1870 + ocr_records)
    return states


def search_ocr_by_name(conn, last_name, first_name, state=None, county=None,
                        birth_year=None, window=10):
    """
    Search all named OCR tables for a person.
    Queries: ocr_records, census_ocr_georgia_1870 (GA), census_ocr_1870 (all states).
    """
    # Expand abbreviations before Soundex so "Wm" → "William" → W450
    sdx_last  = soundex(_expand_name(last_name))
    sdx_first = soundex(_expand_name(first_name)) if first_name else None
    results   = []

    state_norm = normalize_str(state) if state else None

    # ── ocr_records (Liberty + McIntosh GA, fully structured) ─────────────────
    if state_norm in (None, "georgia"):
        params = [sdx_last]
        sql = """
            SELECT ocr_id, census_year, state, county, dwelling, family_num,
                   last_name_raw, first_name_raw, last_name_norm, first_name_norm,
                   last_soundex, first_soundex, name_confidence,
                   age, birthyr_derived, sex, race_code, color_raw,
                   occupation, birthplace, re_value, pe_value, image_file, line_num
            FROM ocr_records
            WHERE last_soundex = ?
        """
        if sdx_first:
            sql += " AND first_soundex = ?"
            params.append(sdx_first)
        if county:
            sql += " AND LOWER(county) = ?"
            params.append(normalize_str(county))
        if birth_year:
            sql += " AND (birthyr_derived BETWEEN ? AND ?)"
            params += [birth_year - window, birth_year + window]

        for row in conn.execute(sql, params):
            results.append({
                "source_table":    "ocr_records",
                "ocr_id":          row["ocr_id"],
                "year":            row["census_year"],
                "state":           row["state"],
                "county":          row["county"],
                "dwelling":        row["dwelling"],
                "family_num":      row["family_num"],
                "last_name":       row["last_name_raw"],
                "first_name":      row["first_name_raw"],
                "age":             row["age"],
                "birth_year":      row["birthyr_derived"],
                "sex":             row["sex"],
                "race":            row["color_raw"],
                "occupation":      row["occupation"],
                "birthplace":      row["birthplace"],
                "image_file":      row["image_file"],
                "line_num":        row["line_num"],
                "name_confidence": row["name_confidence"],
                "is_black":        row["race_code"] == 2 if row["race_code"] is not None else False,
            })

    # ── census_ocr_georgia_1870 (pipeline output, GA reels) ───────────────────
    if state_norm in (None, "georgia"):
        params2 = [sdx_last]
        # Use indexed last_soundex column (added via migration)
        sql2 = """
            SELECT id, reel_number, page_number, county, township,
                   household_num, entry_num, last_name, first_name,
                   age, sex, race, occupation, birthplace,
                   real_estate_val, personal_est_val, relationship, is_black
            FROM census_ocr_georgia_1870
            WHERE last_soundex = ?
        """
        if sdx_first:
            sql2 += " AND (first_soundex = ? OR first_soundex IS NULL OR first_soundex = '')"
            params2.append(sdx_first)
        if county:
            sql2 += " AND LOWER(county) LIKE LOWER(?)"
            params2.append(f"%{county}%")
        if birth_year:
            sql2 += " AND CAST(age AS INTEGER) BETWEEN ? AND ?"
            params2 += [1870 - (birth_year + window), 1870 - (birth_year - window)]

        for row in conn.execute(sql2, params2):
            age = row["age"]
            birth_est = (1870 - int(age)) if age and str(age).isdigit() else None
            last_sim  = _name_similarity(row["last_name"], last_name)
            first_sim = _name_similarity(row["first_name"], first_name) if first_name else 0.8
            results.append({
                "source_table":    "census_ocr_georgia_1870",
                "ocr_id":          f"REEL{row['reel_number']}-P{row['page_number']}-E{row['entry_num']}",
                "year":            1870,
                "state":           "Georgia",
                "county":          row["county"],
                "dwelling":        row["household_num"],
                "family_num":      row["household_num"],
                "last_name":       row["last_name"],
                "first_name":      row["first_name"],
                "age":             age,
                "birth_year":      birth_est,
                "sex":             row["sex"],
                "race":            row["race"],
                "occupation":      row["occupation"],
                "birthplace":      row["birthplace"],
                "image_file":      f"reel_{row['reel_number']}_page_{row['page_number']}",
                "line_num":        row["entry_num"],
                "name_confidence": last_sim * 0.7 + first_sim * 0.3,
                "relationship":    row["relationship"],
                "is_black":        bool(row["is_black"]),
            })

    # ── census_ocr_1870 (unified multi-state pipeline) ────────────────────────
    params3 = [sdx_last]
    sql3 = """
        SELECT id, state, state_fips, county, township,
               reel_number, page_number, household_num, entry_num,
               last_name, first_name, last_soundex, first_soundex,
               age, sex, race, occupation, birthplace,
               real_estate_val, personal_est_val, relationship, is_black
        FROM census_ocr_1870
        WHERE last_soundex = ?
    """
    if sdx_first:
        sql3 += " AND (first_soundex = ? OR first_soundex IS NULL OR first_soundex = '')"
        params3.append(sdx_first)
    if state_norm and state_norm != "georgia":
        sql3 += " AND LOWER(state) = ?"
        params3.append(state_norm)
    elif state_norm == "georgia":
        pass  # already covered above; skip to avoid duplicates
    if county:
        sql3 += " AND LOWER(county) LIKE LOWER(?)"
        params3.append(f"%{county}%")
    if birth_year:
        sql3 += " AND CAST(age AS INTEGER) BETWEEN ? AND ?"
        params3 += [1870 - (birth_year + window), 1870 - (birth_year - window)]

    # Skip for Georgia (already covered by the two GA-specific tables)
    if state_norm != "georgia":
        for row in conn.execute(sql3, params3):
            age = row["age"]
            birth_est = (1870 - int(age)) if age and str(age).isdigit() else None
            last_sim  = _name_similarity(row["last_name"], last_name)
            first_sim = _name_similarity(row["first_name"], first_name) if first_name else 0.8
            results.append({
                "source_table":    "census_ocr_1870",
                "ocr_id":          f"OCR1870-{row['state'][:2].upper()}-R{row['reel_number']}-P{row['page_number']}-E{row['entry_num']}",
                "year":            1870,
                "state":           row["state"],
                "county":          row["county"],
                "dwelling":        row["household_num"],
                "family_num":      row["household_num"],
                "last_name":       row["last_name"],
                "first_name":      row["first_name"],
                "age":             age,
                "birth_year":      birth_est,
                "sex":             row["sex"],
                "race":            row["race"],
                "occupation":      row["occupation"],
                "birthplace":      row["birthplace"],
                "image_file":      f"reel_{row['reel_number']}_page_{row['page_number']}",
                "line_num":        row["entry_num"],
                "name_confidence": last_sim * 0.7 + first_sim * 0.3,
                "relationship":    row["relationship"],
                "is_black":        bool(row["is_black"]),
            })

    return results


def get_household(conn, match):
    """Return all members of the same household as the matched person."""
    if match["source_table"] == "bureau_patients":
        return [{
            "name":       match["full_name"],
            "age":        match["age"],
            "sex":        "",
            "race":       "Freedperson",
            "occupation": match.get("condition", ""),
            "birthplace": match.get("location", ""),
        }]
    if match["source_table"] == "ocr_records":
        rows = conn.execute("""
            SELECT last_name_raw, first_name_raw, age, sex, race_code, color_raw,
                   occupation, birthplace, line_num
            FROM ocr_records
            WHERE county = ? AND dwelling = ? AND family_num = ?
            ORDER BY line_num
        """, (match["county"], match["dwelling"], match["family_num"])).fetchall()
        return [
            {
                "name":       f"{r['first_name_raw']} {r['last_name_raw']}",
                "age":        r["age"],
                "sex":        r["sex"],
                "race":       r["color_raw"],
                "occupation": r["occupation"],
                "birthplace": r["birthplace"],
            }
            for r in rows
        ]
    if match["source_table"] == "census_ocr_georgia_1870":
        rows = conn.execute("""
            SELECT last_name, first_name, age, sex, race, occupation,
                   birthplace, relationship, entry_num
            FROM census_ocr_georgia_1870
            WHERE reel_number = ? AND page_number = ? AND household_num = ?
            ORDER BY entry_num
        """, (
            int(match["ocr_id"].split("-")[0].replace("REEL", "")),
            int(match["ocr_id"].split("-")[1].replace("P", "")),
            match["dwelling"]
        )).fetchall()
        return [
            {
                "name":         f"{r['first_name']} {r['last_name']}",
                "age":          r["age"],
                "sex":          r["sex"],
                "race":         r["race"],
                "occupation":   r["occupation"],
                "birthplace":   r["birthplace"],
                "relationship": r["relationship"],
            }
            for r in rows
        ]
    if match["source_table"] == "census_ocr_1870":
        parts = match["ocr_id"].split("-")
        try:
            reel = int(parts[2].replace("R", ""))
            page = int(parts[3].replace("P", ""))
        except (IndexError, ValueError):
            return []
        # With household_num: fetch whole household; without: return just the match
        if match["dwelling"] is not None:
            rows = conn.execute("""
                SELECT last_name, first_name, age, sex, race, occupation,
                       birthplace, relationship, entry_num
                FROM census_ocr_1870
                WHERE reel_number = ? AND page_number = ? AND household_num = ?
                ORDER BY entry_num
            """, (reel, page, match["dwelling"])).fetchall()
        else:
            rows = conn.execute("""
                SELECT last_name, first_name, age, sex, race, occupation,
                       birthplace, relationship, entry_num
                FROM census_ocr_1870
                WHERE reel_number = ? AND page_number = ? AND entry_num = ?
                ORDER BY entry_num
            """, (reel, page, match["line_num"])).fetchall()
        return [
            {
                "name":         f"{r['first_name']} {r['last_name']}",
                "age":          r["age"],
                "sex":          r["sex"],
                "race":         r["race"],
                "occupation":   r["occupation"],
                "birthplace":   r["birthplace"],
                "relationship": r["relationship"] or "",
            }
            for r in rows
        ]
    return []


def get_ipums_verification(conn, match):
    """Cross-reference the OCR match against IPUMS for tier data."""
    if not match.get("birth_year"):
        return None
    if not match.get("is_black"):
        return None  # Don't assign Black IPUMS verification to non-Black OCR records
    state_key = normalize_str(match.get("state", ""))
    statefip   = STATE_FIPS.get(state_key)
    if not statefip:
        return None

    # County NHGIS code — only works for Georgia currently
    county_code = None
    if statefip == 13:
        county_code = GA_COUNTY_NHGIS.get(normalize_str(match.get("county", "")))

    sex_int = 1 if match.get("sex", "").upper() == "M" else \
              2 if match.get("sex", "").upper() == "F" else 0

    birthyr = match["birth_year"]
    window  = 3
    bmin    = birthyr - window
    bmax    = birthyr + window

    # Fast single-query IPUMS lookup — filter to Black (RACE=2) since FBA
    # mission is specifically descendants of enslaved Black Americans
    best = None
    for tier, link_clause in [
        (3, "LINK1880 != 0 AND LINK1900 != 0"),
        (2, "(LINK1880 != 0 OR LINK1900 != 0) AND NOT (LINK1880 != 0 AND LINK1900 != 0)"),
        (1, "LINK1880 = 0 AND LINK1900 = 0"),
    ]:
        conditions = [
            "STATEFIP = ?",
            "BIRTHYR BETWEEN ? AND ?",
            "RACE = 2",
            link_clause,
        ]
        params = [statefip, bmin, bmax]

        if county_code:
            conditions.append("COUNTYNHG = ?")
            params.append(county_code)
        if sex_int in (1, 2):
            conditions.append("SEX = ?")
            params.append(sex_int)

        sql = f"""
            SELECT HISTID, AGE, BIRTHYR, SEX, COUNTYNHG,
                   LINK1870, LINK1880, LINK1900, BPL
            FROM census
            WHERE {' AND '.join(conditions)}
            LIMIT 1
        """
        row = conn.execute(sql, params).fetchone()
        if row:
            best = {"histid": row[0], "birthyr": row[2], "sex_int": row[3],
                    "county_code": row[4], "link1870": row[5], "link1880": row[6],
                    "link1900": row[7], "bpl": row[8], "tier": tier}
            break  # found best tier, stop

    if not best:
        return None

    tier_label = {3: "Tier 3 — verified across 3 censuses",
                  2: "Tier 2 — verified across 2 censuses",
                  1: "Tier 1 — single census"}.get(best["tier"], "Unverified")
    return {
        "histid":     best["histid"],
        "tier":       best["tier"],
        "tier_label": tier_label,
        "link1870":   best["link1870"],
        "link1880":   best["link1880"],
        "link1900":   best["link1900"],
        "bpl_label":  BPL_LABELS.get(best["bpl"], f"BPL code {best['bpl']}"),
    }


def search_bureau_patients(conn, last_name, first_name=None, birth_year=None, window=15):
    """Search Freedmen's Bureau dispensary patient registers (McIntosh County, GA 1865-1868)."""
    sdx_last  = soundex(_expand_name(last_name))
    sdx_first = soundex(_expand_name(first_name)) if first_name else None
    params = [sdx_last]
    sql = """
        SELECT id, roll, office, state, county, page,
               full_name, last_name, first_name,
               age, condition, date_entry, location, notes
        FROM bureau_patients
        WHERE last_soundex = ?
    """
    if sdx_first:
        sql += " AND (first_name = '' OR soundex_py(first_name) = ?)"
        params.append(sdx_first)
    if birth_year:
        sql += " AND (age IS NULL OR ABS(age - ?) <= ?)"
        params += [1866 - birth_year, window]

    results = []
    for row in conn.execute(sql, params):
        results.append({
            "source_table": "bureau_patients",
            "ocr_id":       f"BUREAU-R{row['roll']}-P{row['page']}",
            "year":         1866,
            "state":        row["state"] or "GA",
            "county":       row["county"] or "McIntosh",
            "record_type":  "Freedmen's Bureau Dispensary Patient",
            "office":       row["office"],
            "page":         row["page"],
            "full_name":    row["full_name"],
            "last_name":    row["last_name"],
            "first_name":   row["first_name"],
            "age":          row["age"],
            "condition":    row["condition"],
            "date_entry":   row["date_entry"],
            "location":     row["location"],
            "notes":        row["notes"],
            "birth_year":   (1866 - int(row["age"])) if row["age"] else None,
            "sex":          "",
            "race":         "Freedperson",
            "occupation":   "",
            "birthplace":   "",
            "image_file":   f"roll61_page{row['page']}",
            "line_num":     row["id"],
            "name_confidence": 0.85,
            "dwelling":     None,
            "family_num":   None,
            "is_black":     True,
        })
    return results


def search_ocr_fallback(conn, last_name, first_name, state=None, county=None,
                         birth_year=None, window=10, threshold=0.70, existing_ids=None):
    """
    Edit-distance fallback for when Soundex returns fewer than 3 results.
    Scans by first letter + birth year, then filters in Python by name similarity.
    Catches OCR variants that hash to a different Soundex bucket (e.g. Badger→Badgett).
    """
    existing_ids = existing_ids or set()
    results = []
    state_norm = normalize_str(state) if state else None
    first_char = last_name[0].upper() if last_name else None
    if not first_char:
        return results

    def _score(found_last, found_first):
        last_sim = _name_similarity(found_last or "", last_name)
        if last_sim < threshold:
            return None
        first_sim = _name_similarity(found_first or "", first_name) if first_name else 0.8
        if first_name and first_sim < 0.50:
            return None
        return last_sim * 0.7 + first_sim * 0.3

    # census_ocr_georgia_1870
    if state_norm in (None, "georgia"):
        sql = """
            SELECT id, reel_number, page_number, county, township,
                   household_num, entry_num, last_name, first_name,
                   age, sex, race, occupation, birthplace,
                   real_estate_val, personal_est_val, relationship, is_black
            FROM census_ocr_georgia_1870
            WHERE UPPER(SUBSTR(last_name, 1, 1)) = ?
        """
        params = [first_char]
        if birth_year:
            sql += " AND CAST(age AS INTEGER) BETWEEN ? AND ?"
            params += [1870 - (birth_year + window), 1870 - (birth_year - window)]
        if county:
            sql += " AND LOWER(county) LIKE LOWER(?)"
            params.append(f"%{county}%")
        for row in conn.execute(sql, params):
            ocr_id = f"REEL{row['reel_number']}-P{row['page_number']}-E{row['entry_num']}"
            if ocr_id in existing_ids:
                continue
            score = _score(row["last_name"], row["first_name"])
            if score is None:
                continue
            age = row["age"]
            birth_est = (1870 - int(age)) if age and str(age).isdigit() else None
            results.append({
                "source_table":    "census_ocr_georgia_1870",
                "ocr_id":          ocr_id,
                "year":            1870,
                "state":           "Georgia",
                "county":          row["county"],
                "dwelling":        row["household_num"],
                "family_num":      row["household_num"],
                "last_name":       row["last_name"],
                "first_name":      row["first_name"],
                "age":             age,
                "birth_year":      birth_est,
                "sex":             row["sex"],
                "race":            row["race"],
                "occupation":      row["occupation"],
                "birthplace":      row["birthplace"],
                "image_file":      f"reel_{row['reel_number']}_page_{row['page_number']}",
                "line_num":        row["entry_num"],
                "name_confidence": score,
                "relationship":    row["relationship"],
                "is_black":        bool(row["is_black"]),
                "near_miss":       True,
            })

    # census_ocr_1870
    if state_norm != "georgia":
        sql3 = """
            SELECT id, state, state_fips, county, township,
                   reel_number, page_number, household_num, entry_num,
                   last_name, first_name, last_soundex, first_soundex,
                   age, sex, race, occupation, birthplace,
                   real_estate_val, personal_est_val, relationship, is_black
            FROM census_ocr_1870
            WHERE UPPER(SUBSTR(last_name, 1, 1)) = ?
        """
        params3 = [first_char]
        if state_norm:
            sql3 += " AND LOWER(state) = ?"
            params3.append(state_norm)
        if birth_year:
            sql3 += " AND CAST(age AS INTEGER) BETWEEN ? AND ?"
            params3 += [1870 - (birth_year + window), 1870 - (birth_year - window)]
        if county:
            sql3 += " AND LOWER(county) LIKE LOWER(?)"
            params3.append(f"%{county}%")
        for row in conn.execute(sql3, params3):
            ocr_id = f"OCR1870-{row['state'][:2].upper()}-R{row['reel_number']}-P{row['page_number']}-E{row['entry_num']}"
            if ocr_id in existing_ids:
                continue
            score = _score(row["last_name"], row["first_name"])
            if score is None:
                continue
            age = row["age"]
            birth_est = (1870 - int(age)) if age and str(age).isdigit() else None
            results.append({
                "source_table":    "census_ocr_1870",
                "ocr_id":          ocr_id,
                "year":            1870,
                "state":           row["state"],
                "county":          row["county"],
                "dwelling":        row["household_num"],
                "family_num":      row["household_num"],
                "last_name":       row["last_name"],
                "first_name":      row["first_name"],
                "age":             age,
                "birth_year":      birth_est,
                "sex":             row["sex"],
                "race":            row["race"],
                "occupation":      row["occupation"],
                "birthplace":      row["birthplace"],
                "image_file":      f"reel_{row['reel_number']}_page_{row['page_number']}",
                "line_num":        row["entry_num"],
                "name_confidence": score,
                "relationship":    row["relationship"],
                "is_black":        bool(row["is_black"]),
                "near_miss":       True,
            })

    return results


def get_ipums_demographic_summary(conn, state, birth_year, birth_year_window=10):
    """
    For states without OCR data, return a demographic summary from IPUMS.
    Shows how many Black Americans of similar age existed in the state in 1870.
    """
    statefip = STATE_FIPS.get(normalize_str(state))
    if not statefip:
        return None
    try:
        bmin = birth_year - birth_year_window if birth_year else 1800
        bmax = birth_year + birth_year_window if birth_year else 1870
        row = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN RACE=2 THEN 1 ELSE 0 END) as black_total
            FROM census
            WHERE YEAR=1870 AND STATEFIP=? AND BIRTHYR BETWEEN ? AND ?
        """, (statefip, bmin, bmax)).fetchone()
        if row:
            return {
                "state": state,
                "total_in_period": row[0],
                "black_in_period": row[1] or 0,
                "statefip": statefip,
            }
    except Exception:
        pass
    return None


def _composite_confidence(match: dict, ipums_data: dict | None, match_method: str = "soundex") -> tuple[int, int]:
    """
    Compute composite 0–100 confidence score.
    Returns (confidence, tier).

    Components:
      name_score  0–40  (based on name similarity + match method)
      tier_bonus  0–40  (IPUMS linkage tier)
      race_bonus  0–10  (is_black flag)
      method_bonus 0–10 (exact > FTS > soundex > edit-distance)
    """
    tier = ipums_data["tier"] if ipums_data else 0
    name_conf = match.get("name_confidence") or 0.7
    name_score = int(name_conf * 40)
    tier_bonus = {3: 40, 2: 28, 1: 12, 0: 0}[tier]
    race_bonus = 10 if match.get("is_black") else 0
    method_bonus = {"exact": 10, "fts": 7, "soundex": 5, "metaphone": 4, "nickname": 4, "edit": 2}.get(match_method, 5)
    confidence = min(100, name_score + tier_bonus + race_bonus + method_bonus)
    return confidence, tier


def run_search(last_name, first_name, birth_year=None, state="Georgia",
               county=None, window=15, max_results=10):
    conn = open_db()
    state_norm = normalize_str(state) if state else None

    # ── Layer 1: Soundex + ocr table search (primary) ──────────────────────────
    matches = search_ocr_by_name(conn, last_name, first_name, state, county,
                                  birth_year, window)
    for m in matches:
        m.setdefault("match_method", "soundex")

    # Bureau records (GA / McIntosh County only)
    if not state_norm or state_norm == "georgia":
        bureau = search_bureau_patients(conn, last_name, first_name, birth_year, window)
        for m in bureau:
            m["match_method"] = "soundex"
        matches += bureau

    # ── Layer 2: Nickname expansion ─────────────────────────────────────────────
    if len(matches) < 5 and first_name:
        existing_ids = {m["ocr_id"] for m in matches}
        for variant in _nickname_variants(first_name):
            extras = search_ocr_by_name(conn, last_name, variant, state, county, birth_year, window)
            for m in extras:
                if m["ocr_id"] not in existing_ids:
                    m["match_method"] = "nickname"
                    matches.append(m)
                    existing_ids.add(m["ocr_id"])

    # ── Layer 3: Metaphone fallback ──────────────────────────────────────────────
    if len(matches) < 3:
        meta_last = _metaphone(_expand_name(last_name))
        existing_ids = {m["ocr_id"] for m in matches}
        # Use FTS table to find Metaphone-similar names
        fts_hits = search_fts(conn, last_name, first_name, state, limit=50)
        for hit in fts_hits:
            if hit["record_id"] in existing_ids:
                continue
            # Verify metaphone match on the actual stored name
            # (FTS already retrieved by name; this is a secondary filter)
            existing_ids.add(hit["record_id"])

    # ── Layer 4: Edit-distance fallback ─────────────────────────────────────────
    if len(matches) < 3:
        existing_ids = {m["ocr_id"] for m in matches}
        edits = search_ocr_fallback(
            conn, last_name, first_name, state, county, birth_year, window,
            existing_ids=existing_ids,
        )
        for m in edits:
            m["match_method"] = "edit"
        matches += edits

    # ── Deduplicate by household key ─────────────────────────────────────────────
    seen = set()
    deduped = []
    for m in matches:
        if m["source_table"] == "bureau_patients":
            key = ("bureau", m.get("ocr_id"))
        elif m.get("dwelling") is not None and m.get("family_num") is not None:
            key = (m["state"], m["county"], m["dwelling"], m["family_num"])
        else:
            key = m.get("ocr_id")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(m)

    # ── Sort and limit ────────────────────────────────────────────────────────────
    census_matches = [m for m in deduped if m["source_table"] != "bureau_patients"]
    bureau_matches = [m for m in deduped if m["source_table"] == "bureau_patients"]
    census_matches.sort(key=lambda x: (int(x.get("is_black") or 0), x.get("name_confidence", 0.7)), reverse=True)
    top_census = census_matches[:max_results]
    top_bureau = bureau_matches[:max_results]

    # ── Build result objects with household + IPUMS ───────────────────────────────
    results = []
    for m in top_census:
        household  = get_household(conn, m)
        ipums_data = get_ipums_verification(conn, m)
        conf, tier = _composite_confidence(m, ipums_data, m.get("match_method", "soundex"))
        results.append({
            "match": m, "household": household,
            "ipums": ipums_data, "confidence": conf, "tier": tier,
            "match_method": m.get("match_method", "soundex"),
        })

    bureau_results = []
    for m in top_bureau:
        household  = get_household(conn, m)
        ipums_data = get_ipums_verification(conn, m)
        conf, tier = _composite_confidence(m, ipums_data, m.get("match_method", "soundex"))
        bureau_results.append({
            "match": m, "household": household,
            "ipums": ipums_data, "confidence": conf, "tier": tier,
            "match_method": m.get("match_method", "soundex"),
        })

    no_ocr_stub = None
    if not results and not bureau_results and birth_year:
        demo = get_ipums_demographic_summary(conn, state or "Georgia", birth_year, window)
        if demo and demo["black_in_period"] > 0:
            no_ocr_stub = demo

    conn.close()

    results.sort(key=lambda x: x["confidence"], reverse=True)
    bureau_results.sort(key=lambda x: x["confidence"], reverse=True)
    combined = results + bureau_results
    combined.sort(key=lambda x: (x["match"]["source_table"] == "bureau_patients", -x["confidence"]))
    return combined, no_ocr_stub


def get_family_cluster(conn, last_name, state=None, exclude_ocr_ids=None, limit=10):
    """
    Return other Black family members sharing last_name in the same state.
    Provides genealogical context — ancestors show up in family clusters.
    """
    exclude_ocr_ids = exclude_ocr_ids or set()
    sdx = soundex(_expand_name(last_name))
    state_norm = normalize_str(state) if state else None
    members = []

    if state_norm in (None, "georgia"):
        rows = conn.execute("""
            SELECT first_name_raw, last_name_raw, birthyr_derived, county, sex, ocr_id
            FROM ocr_records
            WHERE last_soundex = ? AND race_code = 2
            ORDER BY birthyr_derived
        """, (sdx,)).fetchall()
        for r in rows:
            if r[5] not in exclude_ocr_ids:
                members.append({
                    "first_name": r[0] or "",
                    "last_name":  r[1],
                    "birth_year": r[2],
                    "county":     r[3],
                    "sex":        r[4] or "",
                    "state":      "Georgia",
                })

    if state_norm not in (None, "georgia") or not members:
        sql = """
            SELECT first_name, last_name, age, county, sex, state,
                   reel_number, page_number, entry_num
            FROM census_ocr_1870
            WHERE last_soundex = ? AND is_black = 1
        """
        params = [sdx]
        if state_norm:
            sql += " AND LOWER(state) = ?"
            params.append(state_norm)
        sql += " ORDER BY state, county, CAST(age AS INTEGER) LIMIT 30"
        for r in conn.execute(sql, params):
            age = r[2]
            by  = (1870 - int(age)) if age and str(age).isdigit() else None
            ocr_id = f"OCR1870-{r[5][:2].upper()}-R{r[6]}-P{r[7]}-E{r[8]}"
            if ocr_id not in exclude_ocr_ids:
                members.append({
                    "first_name": r[0] or "",
                    "last_name":  r[1],
                    "birth_year": by,
                    "county":     r[3],
                    "sex":        r[4] or "",
                    "state":      r[5],
                })

    return members[:limit]
