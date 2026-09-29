import copy
import zipfile

import pytest

from leadtool import db
from leadtool.config import DEFAULTS
from leadtool.sources import bolagsverket, producthunt
from leadtool.sources.csv_import import read_csv
from leadtool.web import domain_of


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "leads.db")


def test_domain_normalization():
    assert domain_of("https://www.Example.com/pricing?x=1") == "example.com"
    assert domain_of("example.io") == "example.io"
    assert domain_of("") is None


def test_dedupe_on_domain_and_org_nr(conn):
    assert db.insert_lead(conn, db.Lead(name="A", source="import", domain="a.com"))
    assert db.insert_lead(conn, db.Lead(name="A2", source="producthunt", domain="a.com")) is None
    assert db.insert_lead(conn, db.Lead(name="B", source="bolagsverket", org_nr="5561234567"))
    assert db.insert_lead(conn, db.Lead(name="B", source="import", org_nr="5561234567")) is None
    assert len(db.list_leads(conn)) == 2


def test_csv_import_swedish_headers(tmp_path, conn):
    f = tmp_path / "leads.csv"
    f.write_text("företagsnamn;hemsida;beskrivning;org.nr\n"
                 "Acme;www.acme.se;Fakturering;556123-4567\n"
                 "Acme igen;https://acme.se/;;\n"
                 ";;;\n", encoding="utf-8")
    new, dupes = db.save_all(conn, read_csv(f))
    assert (new, dupes) == (1, 1)
    row = db.list_leads(conn)[0]
    assert row["domain"] == "acme.se" and row["org_nr"] == "5561234567"


class FakeResp:
    def __init__(self, data, status=200, headers=None):
        self._data, self.status_code, self.headers = data, status, headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_producthunt_fetch(monkeypatch, conn):
    monkeypatch.setenv("PRODUCTHUNT_TOKEN", "t")
    page = {"data": {"posts": {"pageInfo": {"hasNextPage": False, "endCursor": None},
            "edges": [{"node": {"id": "1", "name": "Shiny", "tagline": "Do X fast",
                                "description": None, "url": "https://www.producthunt.com/posts/shiny",
                                "website": "https://shiny.app/?ref=producthunt",
                                "createdAt": "2026-09-20T07:00:00Z", "featuredAt": None}}]}}}
    calls = []
    monkeypatch.setattr(producthunt.requests, "post",
                        lambda *a, **kw: calls.append(kw["json"]["variables"]) or FakeResp(page))
    cfg = copy.deepcopy(DEFAULTS)
    cfg["sources"]["producthunt"]["topics"] = ["saas", "marketing"]
    new, dupes = db.save_all(conn, producthunt.fetch(cfg, db.known_checker(conn)))
    assert new == 1 and len(calls) == 2  # samma produkt i två topics räknas en gång
    row = db.list_leads(conn)[0]
    assert row["domain"] == "shiny.app" and row["launch_date"] == "2026-09-20"
    # andra körningen: redan känd
    assert db.save_all(conn, producthunt.fetch(cfg, db.known_checker(conn))) == (0, 0)


def test_producthunt_redirect_respects_robots(monkeypatch):
    monkeypatch.setattr(producthunt, "can_fetch", lambda *a, **k: False)
    assert producthunt.resolve_website("https://www.producthunt.com/r/abc", "ua", 1) is None
    monkeypatch.setattr(producthunt, "can_fetch", lambda *a, **k: True)
    monkeypatch.setattr(producthunt.requests, "head",
                        lambda *a, **k: FakeResp({}, 301, {"Location": "https://real.io/?ref=ph"}))
    assert producthunt.resolve_website("https://www.producthunt.com/r/abc", "ua", 1) == "https://real.io/"


def _bulk(tmp_path, header, rows, zipped=False):
    text = "\n".join([header] + rows) + "\n"
    if not zipped:
        p = tmp_path / "bulk.txt"
        p.write_text(text, encoding="utf-8")
        return p
    p = tmp_path / "bulk.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("bolagsdata.txt", text)
    return p


def test_bolagsverket_filters_on_sni_column(tmp_path, conn):
    p = _bulk(tmp_path, "organisationsidentitet;organisationsnamn;organisationsform;avregistreringsdatum;sni",
              ["5560000001;SaaS AB;AB;;62010",
               "5560000002;Bygg AB;AB;;41200",
               "5560000003;Gammal AB;AB;2020-01-01;62010",
               "9696000004;Handelsbolaget;HB;;62010"], zipped=True)
    cfg = copy.deepcopy(DEFAULTS)
    cfg["sources"]["bolagsverket"]["bulk_file"] = str(p)
    new, _ = db.save_all(conn, bolagsverket.fetch(cfg, db.known_checker(conn)))
    assert new == 1 and db.list_leads(conn)[0]["name"] == "SaaS AB"


def test_bolagsverket_keywords_and_api_verification(tmp_path, conn, monkeypatch):
    p = _bulk(tmp_path, "organisationsidentitet;organisationsnamn;organisationsform;verksamhetsbeskrivning",
              ["5560000001;Molnet AB;AB;Utveckling av programvara och SaaS-tjänster",
               "5560000002;Konsult AB;AB;Utveckling av programvara åt kunder",
               "5560000003;Bageri AB;AB;Bageri och kafé"])
    monkeypatch.setenv("BOLAGSVERKET_CLIENT_ID", "id")
    monkeypatch.setenv("BOLAGSVERKET_CLIENT_SECRET", "s")
    sni = {"5560000001": "58290", "5560000002": "62020"}

    def fake_post(url, **kw):
        if "token" in url:
            return FakeResp({"access_token": "tok"})
        org = kw["json"]["identitetsbeteckning"]
        return FakeResp({"organisationer": [{"naringsgrenOrganisation": {"sni": [{"kod": sni[org]}]}}]})

    monkeypatch.setattr(bolagsverket.requests, "post", fake_post)
    cfg = copy.deepcopy(DEFAULTS)
    cfg["sources"]["bolagsverket"]["bulk_file"] = str(p)
    new, _ = db.save_all(conn, bolagsverket.fetch(cfg, db.known_checker(conn)))
    assert new == 1 and db.list_leads(conn)[0]["org_nr"] == "5560000001"


def test_org_nr_normalization():
    assert bolagsverket.normalize_org_nr("556123-4567") == "5561234567"
    assert bolagsverket.normalize_org_nr("165561234567") == "5561234567"
    assert bolagsverket.normalize_org_nr("123") is None
