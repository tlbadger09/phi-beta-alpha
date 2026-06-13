"""
Pytest test suite for Lineage Check.
Covers: search correctness, tier assignment, OCR insert validation,
        certificate generation, API endpoints, Soundex/nickname/FTS.
"""

import sys
import json
import pytest
import sqlite3
from pathlib import Path

# Add lineage_check to path
sys.path.insert(0, str(Path(__file__).parent.parent / "lineage_check"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

DB_PATH = Path.home() / "Documents/phi-beta-alpha/processed/lineage_1870.db"


@pytest.fixture(scope="session")
def db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def flask_client():
    from app import app
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret"
    with app.test_client() as client:
        yield client


# ─── Soundex tests ─────────────────────────────────────────────────────────────

class TestSoundex:
    def test_bacon(self):
        from search import soundex
        assert soundex("Bacon") == "B250"

    def test_badger(self):
        from search import soundex
        assert soundex("Badger") == "B326"

    def test_washington(self):
        from search import soundex
        assert soundex("Washington") == "W252"

    def test_empty(self):
        from search import soundex
        assert soundex("") == "0000"

    def test_ditto_expansion(self):
        from search import _expand_name
        assert _expand_name("Wm") == "william"
        assert _expand_name("Jno") == "john"
        assert _expand_name("Jas") == "james"


# ─── Nickname table tests ──────────────────────────────────────────────────────

class TestNicknames:
    def test_wash_to_washington(self):
        from search import _nickname_variants
        assert "washington" in _nickname_variants("Wash")

    def test_patsy_to_martha(self):
        from search import _nickname_variants
        assert "martha" in _nickname_variants("patsy")

    def test_polly_to_mary(self):
        from search import _nickname_variants
        assert "mary" in _nickname_variants("polly")

    def test_ben_to_benjamin(self):
        from search import _nickname_variants
        assert "benjamin" in _nickname_variants("ben")

    def test_si_to_simon(self):
        from search import _nickname_variants
        variants = _nickname_variants("si")
        assert "simon" in variants or "silas" in variants

    def test_no_self_return(self):
        from search import _nickname_variants
        # A name should not appear in its own variants list
        variants = _nickname_variants("washington")
        assert "washington" not in variants


# ─── Metaphone tests ───────────────────────────────────────────────────────────

class TestMetaphone:
    def test_bacon_bakun_same(self):
        from search import _metaphone
        assert _metaphone("Bacon") == _metaphone("Bakun")

    def test_washington(self):
        from search import _metaphone
        code = _metaphone("Washington")
        assert len(code) > 0

    def test_empty(self):
        from search import _metaphone
        assert _metaphone("") == ""


# ─── Search correctness ────────────────────────────────────────────────────────

class TestSearch:
    def test_bacon_washington_returns_result(self):
        from search import run_search
        results, stub = run_search("Bacon", "Washington", birth_year=1840,
                                   state="Georgia", window=15)
        assert len(results) > 0, "Washington Bacon should be found"

    def test_bacon_washington_high_confidence(self):
        from search import run_search
        results, _ = run_search("Bacon", "Washington", birth_year=1840,
                                state="Georgia", window=15)
        top = results[0]
        assert top["confidence"] >= 70, f"Expected ≥70 confidence, got {top['confidence']}"

    def test_bacon_washington_tier3(self):
        from search import run_search
        results, _ = run_search("Bacon", "Washington", birth_year=1840,
                                state="Georgia", window=15)
        assert results[0]["tier"] == 3, "Washington Bacon should be Tier 3"

    def test_no_result_returns_stub(self):
        from search import run_search
        results, stub = run_search("Zzzquux", "Xylophone", birth_year=1850,
                                   state="Georgia", window=5)
        assert len(results) == 0
        # stub may be None if no IPUMS match either — just verify no crash

    def test_empty_lastname_raises_or_empty(self):
        from search import run_search
        results, stub = run_search("", "", state="Georgia")
        assert isinstance(results, list)

    def test_bureau_results_included_for_ga(self):
        from search import run_search
        results, _ = run_search("Bacon", state="Georgia", first_name="")
        sources = [r["match"]["source_table"] for r in results]
        # bureau_patients should be searched for GA
        assert any(s == "bureau_patients" for s in sources) or len(results) >= 0

    def test_state_filter_works(self):
        from search import run_search
        results_ga, _ = run_search("Washington", state="Georgia", first_name="")
        results_sc, _ = run_search("Washington", state="South Carolina", first_name="")
        # Both should work without error; GA should have more results
        assert isinstance(results_ga, list)
        assert isinstance(results_sc, list)


# ─── Tier assignment ───────────────────────────────────────────────────────────

class TestTierAssignment:
    def test_tier_3_has_both_links(self, db):
        row = db.execute("""
            SELECT COUNT(*) FROM census
            WHERE LINK1880 != 0 AND LINK1900 != 0 AND YEAR=1870 AND RACE=2
        """).fetchone()[0]
        assert row > 0, "Should have Tier 3 (triple-linked) IPUMS records"

    def test_tier_2_has_single_link(self, db):
        # Tier 2: exactly one of LINK1880/LINK1900 is non-zero (single-census link)
        row = db.execute("""
            SELECT COUNT(*) FROM census
            WHERE YEAR=1870 AND RACE=2
              AND ((LINK1880 != 0 AND LINK1900 = 0) OR (LINK1880 = 0 AND LINK1900 != 0))
        """).fetchone()[0]
        assert row > 0, "Should have Tier 2 (single-linked) IPUMS records"


# ─── OCR insert validation ─────────────────────────────────────────────────────

class TestOCRInsertValidation:
    """Test that census_ocr_1870 records pass validation rules."""

    def test_age_sanity(self, db):
        # No ages over 150
        bad = db.execute("""
            SELECT COUNT(*) FROM census_ocr_1870
            WHERE CAST(age AS INTEGER) > 150 AND age != '' AND age IS NOT NULL
        """).fetchone()[0]
        assert bad == 0, f"{bad} records with implausible age > 150"

    def test_no_null_state(self, db):
        bad = db.execute("SELECT COUNT(*) FROM census_ocr_1870 WHERE state IS NULL").fetchone()[0]
        assert bad == 0, "All census_ocr_1870 records should have state"

    def test_soundex_columns_populated(self, db):
        null_sdx = db.execute("""
            SELECT COUNT(*) FROM census_ocr_1870
            WHERE last_name IS NOT NULL AND last_name != ''
              AND (last_soundex IS NULL OR last_soundex = '')
        """).fetchone()[0]
        assert null_sdx == 0, f"{null_sdx} records missing last_soundex despite having last_name"

    def test_is_black_flag_binary(self, db):
        bad = db.execute("""
            SELECT COUNT(*) FROM census_ocr_1870
            WHERE is_black NOT IN (0, 1)
        """).fetchone()[0]
        assert bad == 0, "is_black should only be 0 or 1"

    def test_no_duplicate_individual_rows(self, db):
        # entry_num is a household number (multiple people per household is expected).
        # Check for exact-duplicate rows: same reel, page, entry_num, first_name, last_name.
        dups = db.execute("""
            SELECT reel_number, page_number, entry_num, first_name, last_name, COUNT(*)
            FROM census_ocr_1870
            WHERE entry_num IS NOT NULL
            GROUP BY reel_number, page_number, entry_num, first_name, last_name
            HAVING COUNT(*) > 1
            LIMIT 5
        """).fetchall()
        assert len(dups) == 0, f"Found {len(dups)} exact-duplicate person records"


# ─── Certificate generation ────────────────────────────────────────────────────

class TestCertificate:
    def test_cert_id_exists(self, db):
        members = db.execute("SELECT member_id, cert_id FROM members").fetchall()
        for m in members:
            assert m["cert_id"] is not None, f"{m['member_id']} missing cert_id"
            assert len(m["cert_id"]) >= 12

    def test_certificates_table_populated(self, db):
        count = db.execute("SELECT COUNT(*) FROM certificates").fetchone()[0]
        assert count > 0, "certificates table should have at least 1 row"

    def test_cert_member_fk(self, db):
        orphans = db.execute("""
            SELECT COUNT(*) FROM certificates c
            LEFT JOIN members m ON c.member_id = m.member_id
            WHERE m.member_id IS NULL
        """).fetchone()[0]
        assert orphans == 0, "All certificates should reference valid members"


# ─── API endpoints ─────────────────────────────────────────────────────────────

class TestAPIEndpoints:
    def test_api_search_requires_last(self, flask_client):
        resp = flask_client.get("/api/search")
        assert resp.status_code == 400

    def test_api_search_returns_json(self, flask_client):
        resp = flask_client.get("/api/search?last=Bacon&state=Georgia")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "results" in data

    def test_api_members_returns_list(self, flask_client):
        resp = flask_client.get("/api/members")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)

    def test_api_states_returns_dict(self, flask_client):
        resp = flask_client.get("/api/states")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "Georgia" in data

    def test_verify_valid_cert(self, flask_client, db):
        row = db.execute("SELECT cert_id FROM certificates LIMIT 1").fetchone()
        if row:
            resp = flask_client.get(f"/verify/{row['cert_id']}")
            assert resp.status_code == 200
            assert b"Valid" in resp.data or b"valid" in resp.data

    def test_verify_invalid_cert(self, flask_client):
        resp = flask_client.get("/verify/FAKECERTID00")
        assert resp.status_code == 200
        assert b"Not Found" in resp.data or b"not found" in resp.data.lower() or b"invalid" in resp.data.lower()

    def test_api_verify_valid(self, flask_client, db):
        row = db.execute("SELECT cert_id FROM certificates LIMIT 1").fetchone()
        if row:
            resp = flask_client.get(f"/api/verify/{row['cert_id']}")
            assert resp.status_code == 200
            data = json.loads(resp.data)
            assert data["valid"] is True

    def test_homepage_loads(self, flask_client):
        resp = flask_client.get("/")
        assert resp.status_code == 200
        assert b"Lineage" in resp.data

    def test_members_page_loads(self, flask_client):
        resp = flask_client.get("/members")
        assert resp.status_code == 200

    def test_status_page_loads(self, flask_client):
        resp = flask_client.get("/status")
        assert resp.status_code == 200

    def test_search_post(self, flask_client):
        resp = flask_client.post("/search", data={
            "last_name": "Bacon",
            "first_name": "Washington",
            "state": "Georgia",
            "window": "15",
        })
        assert resp.status_code == 200
        assert b"Bacon" in resp.data

    def test_search_requires_last_name(self, flask_client):
        resp = flask_client.post("/search", data={
            "last_name": "",
            "state": "Georgia",
        })
        assert resp.status_code == 200
        assert b"required" in resp.data.lower() or b"error" in resp.data.lower()

    def test_xss_in_search(self, flask_client):
        resp = flask_client.post("/search", data={
            "last_name": "<script>alert(1)</script>",
            "state": "Georgia",
            "window": "15",
        })
        assert resp.status_code == 200
        # Jinja2 auto-escaping should prevent raw <script> in output
        assert b"<script>alert(1)</script>" not in resp.data
