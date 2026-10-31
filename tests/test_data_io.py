"""CSV export/import, global search, and duplicate warnings."""

import csv
import io
import zipfile

from vc_crm.search import normalize, similar

from .conftest import signed_in


def rows_of(text):
    return list(csv.DictReader(io.StringIO(text)))


def setup_two_funds(client):
    f1 = client.post("/api/funds", json={"name": "Fund I"}).json()
    f2 = client.post("/api/funds", json={"name": "Fund II"}).json()
    lp1 = client.post("/api/lps", json={"name": "Alpha Endowment", "lp_type": "Endowment"}).json()
    lp2 = client.post("/api/lps", json={"name": "Beta Family Office"}).json()
    client.post("/api/commitments", json={"lp_id": lp1["id"], "fund_id": f1["id"],
                                          "target_amount": 5_000_000})
    client.post("/api/commitments", json={"lp_id": lp2["id"], "fund_id": f2["id"]})
    return f1, f2, lp1, lp2


def fund_user(client, app, database_url, fund, level="editor", role="viewer"):
    u = signed_in(app, database_url, "u@firm.com", role)
    uid = u.get("/api/auth/me").json()["id"]
    client.put(f"/api/users/{uid}/funds", json={str(fund["id"]): level})
    return u


def test_export_csv_uses_names_and_dollars(client):
    setup_two_funds(client)
    r = client.get("/api/export/commitments.csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = rows_of(r.text)
    first = next(x for x in rows if x["LP"] == "Alpha Endowment")
    assert first["Fund"] == "Fund I" and float(first["Target ask"]) == 5_000_000


def test_export_respects_fund_access(client, app, database_url):
    f1, f2, *_ = setup_two_funds(client)
    u = fund_user(client, app, database_url, f2, "viewer")
    assert [x["Fund"] for x in rows_of(u.get("/api/export/commitments.csv").text)] == ["Fund II"]
    assert [x["LP name"] for x in rows_of(u.get("/api/export/lps.csv").text)] == ["Beta Family Office"]
    assert u.get("/api/export/all.zip").status_code == 403


def test_export_all_zip(client):
    setup_two_funds(client)
    r = client.get("/api/export/all.zip")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"lps.csv", "commitments.csv", "funds.csv", "capital_events.csv",
            "capital_event_allocations.csv", "change_log.csv"} <= names
    assert "users.csv" not in names  # no password hashes or secrets
    assert "Alpha Endowment" in z.read("lps.csv").decode()


def test_import_preview_and_commit(client):
    setup_two_funds(client)
    text = ("lp name,TYPE,Location,Ignored column\n"
            "Gamma Pension,pension,Boston,x\n"
            "Delta Foundation,Not a type,NYC,x\n"
            "The Alpha Endowment Inc.,Endowment,,x\n"
            "Epsilon Trust,,,x\n"
            "Epsilon Trust,,,x\n")
    prev = client.post("/api/import/lps", json={"csv": text}).json()
    assert prev["ignored_columns"] == ["Ignored column"]
    status = {r["row"]: (r["status"], r["message"]) for r in prev["rows"]}
    assert status[2][0] == "ok"
    assert status[3][0] == "error" and "Type must be one of" in status[3][1]
    assert status[4] == ("duplicate", "Looks like existing Alpha Endowment")
    assert status[5][0] == "ok" and status[6] == ("duplicate", "Same name as row 5")
    assert prev["counts"] == {"ok": 2, "duplicate": 2, "error": 1}
    assert len(client.get("/api/lps").json()) == 2  # preview writes nothing
    # Errors block the import unless explicitly skipped.
    assert client.post("/api/import/lps", json={"csv": text, "commit": True}).status_code == 400
    done = client.post("/api/import/lps", json={"csv": text, "commit": True,
                                                "skip_errors": True}).json()
    assert done["imported"] == 2
    lps = {x["name"]: x for x in client.get("/api/lps").json()}
    assert lps["Gamma Pension"]["lp_type"] == "Pension" and lps["Gamma Pension"]["location"] == "Boston"
    log = client.get(f"/api/audit?entity=lps&entity_id={lps['Gamma Pension']['id']}").json()
    assert log[0]["changes"]["imported"] is True


def test_import_links_by_name_and_parses_values(client):
    f1, f2, lp1, lp2 = setup_two_funds(client)
    text = ("LP,Fund,Stage,Target ask,Committed,Close date\n"
            "Beta Family Office,Fund I,closed,\"$2,500,000\",\"2,500,000\",3/15/2026\n"
            "Nobody,Fund I,Prospect,1,,\n")
    prev = client.post("/api/import/commitments", json={"csv": text}).json()
    assert prev["rows"][1]["status"] == "error" and "no match for 'Nobody'" in prev["rows"][1]["message"]
    client.post("/api/import/commitments", json={"csv": text, "commit": True, "skip_errors": True})
    c = [x for x in client.get(f"/api/commitments?lp_id={lp2['id']}").json() if x["fund_id"] == f1["id"]][0]
    assert (c["stage"], c["target_amount"], c["committed_amount"], c["close_date"]) == (
        "Closed", 2_500_000, 2_500_000, "2026-03-15")


def test_import_checks_fund_permissions(client, app, database_url):
    f1, f2, *_ = setup_two_funds(client)
    u = fund_user(client, app, database_url, f2, "editor")
    text = "LP,Fund\nBeta Family Office,Fund I\n"
    r = u.post("/api/import/commitments", json={"csv": text}).json()
    # Fund I is invisible to this user, so it does not even match by name.
    assert r["rows"][0]["status"] == "error" and "no match for 'Fund I'" in r["rows"][0]["message"]
    assert u.post("/api/import/funds", json={"csv": "Fund name\nFund III\n"}).json()["rows"][0][
        "status"] == "error"


def test_import_rejects_bad_files(client):
    assert client.post("/api/import/lps", json={"csv": "only a header\n"}).status_code == 400
    assert client.post("/api/import/lps", json={"csv": "foo,bar\n1,2\n"}).status_code == 400
    assert client.post("/api/import/users", json={"csv": "a\nb\n"}).status_code == 404
    t = client.get("/api/import/lps/template.csv").text
    assert t.startswith('"LP name","Type"')


def test_search(client, app, database_url):
    f1, f2, *_ = setup_two_funds(client)
    client.post("/api/companies", json={"name": "Alphabet Robotics", "sector": "Robotics"})
    client.post("/api/investors", json={"name": "Alpine Ventures"})
    hits = {(h["entity"], h["title"]) for h in client.get("/api/search?q=alp").json()}
    assert hits == {("lps", "Alpha Endowment"), ("companies", "Alphabet Robotics"),
                    ("investors", "Alpine Ventures")}
    assert client.get("/api/search?q=a").json() == []
    u = fund_user(client, app, database_url, f2, "viewer")
    assert ("lps", "Alpha Endowment") not in {(h["entity"], h["title"])
                                              for h in u.get("/api/search?q=alp").json()}


def test_duplicate_check(client, app, database_url):
    f1, f2, *_ = setup_two_funds(client)
    assert normalize("The Alpha Endowment, Inc.") == "alpha endowment"
    assert similar("Alpha Endowmnet", "Alpha Endowment") and not similar("Alpha", "Omega Partners")
    r = client.get("/api/duplicates?entity=lps&name=alpha endowment llc").json()
    assert [x["name"] for x in r["visible"]] == ["Alpha Endowment"] and r["hidden_count"] == 0
    u = fund_user(client, app, database_url, f2, "editor")
    r = u.get("/api/duplicates?entity=lps&name=Alpha Endowment").json()
    assert r == {"visible": [], "hidden_count": 1}  # exists, but no name leaked
    assert client.get("/api/duplicates?entity=rounds&name=x").status_code == 400
