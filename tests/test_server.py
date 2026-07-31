"""
Server tests: criteria intake, and the rules around sending.

    python -m tests.test_server

Kept out of `evals/` on purpose. The eval suite must run with FastAPI absent --
it grades the agent, and the agent has no web dependency. These tests need the
app, so they live here and are run separately.

The send tests are the point of this file. Sending is the one irreversible
thing this system can do, so the things that must be true are asserted, not
assumed:

  - a recipient that is not the one on the venue's page is refused
  - a venue with no extracted email offers no send path at all
  - a mock-mode send writes a draft, records itself, and OPENS NO SOCKET
    (enforced here by blocking outbound connections for the duration)
  - the same venue cannot be sent to twice, even across separate runs
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("AGENT_MODE", "mock")

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from src import sending  # noqa: E402

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not condition else ""))


_LOOPBACK = {"127.0.0.1", "localhost", "::1", "", None}


class no_network:
    """Any attempt to reach a host off this machine inside this block is an error.

    This is how "mock mode touches no network" stops being a claim in a
    docstring and becomes something the test suite enforces.

    Scoped to outbound *connections* rather than socket creation: asyncio builds
    a self-pipe with socket.socketpair() just to exist, and blocking that would
    catch the test harness rather than the code under test. Reaching Gmail --
    or anything else off-box -- requires resolving a name or connecting to a
    non-loopback address, and both of those raise here.
    """

    def __enter__(self):
        self._getaddrinfo = socket.getaddrinfo
        self._create_connection = socket.create_connection

        def guarded_getaddrinfo(host, *a, **k):
            if host not in _LOOPBACK:
                raise AssertionError(f"mock mode tried to resolve {host!r}")
            return self._getaddrinfo(host, *a, **k)

        def guarded_create_connection(address, *a, **k):
            host = address[0] if isinstance(address, tuple) else address
            if host not in _LOOPBACK:
                raise AssertionError(f"mock mode tried to connect to {host!r}")
            return self._create_connection(address, *a, **k)

        socket.getaddrinfo = guarded_getaddrinfo
        socket.create_connection = guarded_create_connection
        return self

    def __exit__(self, *exc):
        socket.getaddrinfo = self._getaddrinfo
        socket.create_connection = self._create_connection
        return False


VENUE_WITH_EMAIL = "https://example.com/casa-jaguar"
VENUE_NO_EMAIL = "https://example.com/sedona-sky-ranch"


def make_fixture_run(runs_dir: str) -> str:
    """A minimal run: one venue with an email, one without."""
    run_dir = os.path.join(runs_dir, "run_20260101_000000")
    os.makedirs(run_dir, exist_ok=True)
    report = {
        "recommended": [], "escalations": [], "duplicates": [],
        "summary": {"regions": 1, "venues_evaluated": 2, "recommended": 2,
                    "escalated": 0, "duplicates_collapsed": 0, "steps": 6,
                    "cost": {"usd_total": 0.0, "calls": 0, "tokens_in": 0,
                             "tokens_out": 0, "by_model": {}},
                    "cost_per_venue_usd": 0.0},
        "all_scored": [
            {"decision": "recommend", "score": 0.9, "confidence": 0.9,
             "rationale": "r", "record": {
                 "name": "Casa Jaguar Tulum", "url": VENUE_WITH_EMAIL,
                 "region": "Tulum, Mexico", "classification": "venue",
                 "contact_method": "email",
                 "contact_value": "weddings@casajaguar.example",
                 "pricing_signal": "request_only"}},
            {"decision": "recommend", "score": 0.8, "confidence": 0.9,
             "rationale": "r", "record": {
                 "name": "Sedona Sky Ranch", "url": VENUE_NO_EMAIL,
                 "region": "Sedona, Arizona", "classification": "venue",
                 "contact_method": "form", "contact_value": None,
                 "pricing_signal": "request_only"}},
        ],
    }
    with open(os.path.join(run_dir, "report.json"), "w") as f:
        json.dump(report, f)
    return run_dir


def main():
    tmp = tempfile.mkdtemp(prefix="server_test_")
    runs_dir = os.path.join(tmp, "runs")
    run_dir = make_fixture_run(runs_dir)

    # Point the app at throwaway paths -- never at the real criteria.json.
    criteria_path = os.path.join(tmp, "criteria.json")
    shutil.copy(os.path.join(ROOT, "criteria.json"), criteria_path)
    server.CRITERIA_PATH = criteria_path
    server.RUNS_DIR = runs_dir
    # The sent ledger deliberately lives outside runs/; point it at the temp
    # dir so a test run never touches the repo's real ledger.
    os.environ["SENT_LEDGER_PATH"] = os.path.join(tmp, "sent_log.json")

    client = TestClient(app=server.app, base_url="http://127.0.0.1:8000")
    hdr = {"Origin": "http://127.0.0.1:8000"}
    base = json.load(open(criteria_path))

    print("\ncriteria intake")
    r = client.get("/criteria")
    check("GET /criteria returns current criteria", r.status_code == 200 and "criteria" in r.json())

    good = dict(base, guest_count=120, budget_ceiling_usd=55000)
    r = client.post("/criteria", json={"criteria": good}, headers=hdr)
    check("valid criteria accepted", r.status_code == 200 and r.json()["saved"] is True, r.text[:120])
    check("saved criteria are returned",
          r.status_code == 200 and r.json()["criteria"]["guest_count"] == 120)
    check("POST /criteria does not auto-run", "run" not in r.json())

    for label, payload in [
        ("budget zero", dict(good, budget_ceiling_usd=0)),
        ("budget negative", dict(good, budget_ceiling_usd=-1)),
        ("budget non-numeric", dict(good, budget_ceiling_usd="40,000")),
        ("guest_count zero", dict(good, guest_count=0)),
        ("guest_count fractional", dict(good, guest_count=85.5)),
        ("guest_count bool", dict(good, guest_count=True)),
        ("regions empty", dict(good, regions=[])),
        ("unknown field", dict(good, auto_send=True)),
    ]:
        r = client.post("/criteria", json={"criteria": payload}, headers=hdr)
        check(f"malformed rejected: {label}", r.status_code == 422 and r.json()["saved"] is False)

    r = client.post("/criteria", json={"criteria": dict(good, regions=["Atlantis, Nowhere"])}, headers=hdr)
    check("unknown region rejected", r.status_code == 422)
    check("unknown region message names it and lists known ones",
          r.status_code == 422 and "Atlantis, Nowhere" in " ".join(r.json()["errors"])
          and "Tulum, Mexico" in " ".join(r.json()["errors"]))

    # criteria.json must be untouched by every rejected attempt above
    check("rejected criteria never written",
          json.load(open(criteria_path))["guest_count"] == 120)

    print("\nauth status")
    r = client.get("/auth/status")
    body = r.json()
    check("GET /auth/status responds", r.status_code == 200)
    check("mock mode reports not connected", body["connected"] is False)
    check("scope is gmail.send only",
          body["scopes"] == ["https://www.googleapis.com/auth/gmail.send"], str(body["scopes"]))

    print("\nsend: recipient must come from the venue record")
    r = client.post("/send", json={
        "url": VENUE_WITH_EMAIL, "to": "attacker@evil.example",
        "subject": "Wedding inquiry", "body": "hello"}, headers=hdr)
    check("send to a recipient not in the record is refused", r.status_code == 422 and r.json()["sent"] is False)
    check("refusal explains the mismatch",
          r.status_code == 422 and "does not match" in r.json()["error"], r.text[:160])

    r = client.post("/send", json={
        "url": VENUE_NO_EMAIL, "to": "someone@example.com",
        "subject": "Wedding inquiry", "body": "hello"}, headers=hdr)
    check("venue with no extracted email cannot be sent to", r.status_code == 422)

    r = client.post("/send", json={
        "url": "https://example.com/not-in-this-run", "to": "a@b.example",
        "subject": "s", "body": "b"}, headers=hdr)
    check("unknown venue url is refused", r.status_code == 404)

    check("no draft written by any refused send",
          not os.path.exists(os.path.join(run_dir, "drafts")))
    check("no sent log written by any refused send",
          not os.path.exists(sending.ledger_path()))

    print("\nsend: mock mode writes a draft and opens no socket")
    with no_network():
        r = client.post("/send", json={
            "url": VENUE_WITH_EMAIL, "to": "weddings@casajaguar.example",
            "subject": "Wedding inquiry - 85 guests",
            "body": "Hi Casa Jaguar team, ..."}, headers=hdr)
    check("mock send succeeds", r.status_code == 200 and r.json()["sent"] is True, r.text[:160])
    check("mock send reports mode=mock", r.status_code == 200 and r.json()["mode"] == "mock")
    check("mock send carries a timestamp", r.status_code == 200 and bool(r.json().get("sent_at")))

    draft = os.path.join(run_dir, "drafts", "casa-jaguar.sent.txt")
    check("mock send wrote a draft file", os.path.exists(draft))
    if os.path.exists(draft):
        text = open(draft).read()
        check("draft records the approved recipient", "weddings@casajaguar.example" in text)
        check("draft is marked as a demo send", "mock" in text.lower())

    log = sending.read_sent_log()
    check("sent log records the venue", VENUE_WITH_EMAIL in log)
    check("sent log stores the approved recipient",
          log.get(VENUE_WITH_EMAIL, {}).get("to") == "weddings@casajaguar.example")

    r = client.post("/send", json={
        "url": VENUE_WITH_EMAIL, "to": "weddings@casajaguar.example",
        "subject": "again", "body": "again"}, headers=hdr)
    check("a second send to the same venue is refused", r.status_code == 422)

    # A rerun produces a NEW run directory. "Already contacted" must survive
    # that, or every rerun would offer to email the same venue again.
    second_run = os.path.join(runs_dir, "run_20260202_000000")
    os.makedirs(second_run, exist_ok=True)
    shutil.copy(os.path.join(run_dir, "report.json"),
                os.path.join(second_run, "report.json"))
    check("the newest run is the one served", server.latest_run_dir() == second_run)
    r = client.post("/send", json={
        "url": VENUE_WITH_EMAIL, "to": "weddings@casajaguar.example",
        "subject": "after a rerun", "body": "after a rerun"}, headers=hdr)
    check("send is still refused after a rerun (ledger outlives the run)",
          r.status_code == 422, r.text[:120])
    r = client.get("/report")
    check("a fresh run still shows the venue as already sent",
          r.status_code == 200 and VENUE_WITH_EMAIL in r.json().get("sent", {}))

    print("\nreport shape")
    r = client.get("/report")
    check("GET /report returns the render shape",
          r.status_code == 200 and {"criteria", "summary", "recommended",
                                    "escalations", "points", "arcs"} <= set(r.json()))
    check("GET /report includes sent state", r.status_code == 200 and VENUE_WITH_EMAIL in r.json()["sent"])

    print("\ncross-origin")
    r = client.post("/send", json={"url": VENUE_WITH_EMAIL, "to": "x@y.example",
                                   "subject": "s", "body": "b"},
                    headers={"Origin": "https://evil.example"})
    check("cross-origin send refused", r.status_code == 403)

    # The origin check must key on the HOST, not a hardcoded port -- the port is
    # a runtime choice (uvicorn --port). Pinning it 403'd every write whenever
    # the server ran anywhere other than 8000.
    for origin in ("http://127.0.0.1:8420", "http://localhost:3000", "http://127.0.0.1:8000"):
        r = client.post("/criteria", json={"criteria": good}, headers={"Origin": origin})
        check(f"loopback origin allowed on any port: {origin}", r.status_code == 200, r.text[:100])
    for origin in ("http://evil.example:8420", "http://127.0.0.1.evil.example:8000"):
        r = client.post("/criteria", json={"criteria": good}, headers={"Origin": origin})
        check(f"non-loopback origin still refused: {origin}", r.status_code == 403)

    shutil.rmtree(tmp, ignore_errors=True)

    # --- read-only demo mode ---------------------------------------------
    # A public deployment has no socket boundary left, so the method IS the
    # boundary. These assert the three dangerous routes are actually closed --
    # hiding the buttons in the UI is not a control, since anyone can post to
    # the route directly.
    import importlib
    os.environ["DEMO_READONLY"] = "1"
    ro = importlib.reload(server)
    ro_client = TestClient(ro.app)

    for path, payload in (("/criteria", {"criteria": good}), ("/run", {}),
                          ("/send", {"url": "https://example.com/casa-jaguar"})):
        r = ro_client.post(path, json=payload, headers={"Origin": "http://localhost:8420"})
        check(f"read-only demo refuses POST {path}", r.status_code == 403, r.text[:120])

    for path in ("/", "/report", "/steps", "/prompts", "/criteria"):
        r = ro_client.get(path)
        check(f"read-only demo still serves GET {path}", r.status_code == 200, str(r.status_code))

    # Same-origin must be accepted on a deployed host, or the site's own fetches
    # would 403 against their own domain.
    r = ro_client.get("/report", headers={"Origin": "https://wedding-agent.up.railway.app",
                                          "Host": "wedding-agent.up.railway.app"})
    check("same-origin allowed on a deployed host", r.status_code == 200, str(r.status_code))

    # And a genuinely foreign origin is still refused there.
    r = ro_client.get("/report", headers={"Origin": "https://evil.example",
                                          "Host": "wedding-agent.up.railway.app"})
    check("foreign origin refused on a deployed host", r.status_code == 403, str(r.status_code))

    os.environ.pop("DEMO_READONLY", None)
    importlib.reload(server)

    # --- read-only defaults follow the BIND, not a remembered flag --------
    # Requiring an explicit DEMO_READONLY meant forgetting it crash-looped the
    # container: a mistake took the whole site down. These pin the rule that a
    # routable bind is read-only unless someone opts out on purpose.
    def load_server(**env):
        """Import server.py fresh under a specific environment."""
        import importlib.util
        keep = {k: os.environ.get(k) for k in ("PORT", "DEMO_READONLY", "CRITERIA_SERVER_HOST")}
        for k in keep:
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in env.items()})
        try:
            spec = importlib.util.spec_from_file_location("s_probe", os.path.join(ROOT, "server.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod, None
        except SystemExit as e:
            return None, str(e)
        finally:
            for k in ("PORT", "DEMO_READONLY", "CRITERIA_SERVER_HOST"):
                os.environ.pop(k, None)
            for k, v in keep.items():
                if v is not None:
                    os.environ[k] = v

    m, _ = load_server()
    check("no env: binds loopback, writes enabled", m and m.HOST == "localhost" and not m.DEMO_READONLY)

    m, _ = load_server(PORT="8080")
    check("PaaS $PORT alone: routable bind defaults to READ-ONLY",
          m is not None and m.HOST == "0.0.0.0" and m.DEMO_READONLY is True)

    m, _ = load_server(PORT="8080", DEMO_READONLY="1")
    check("PaaS + explicit DEMO_READONLY=1 stays read-only",
          m is not None and m.DEMO_READONLY is True)

    m, err = load_server(PORT="8080", DEMO_READONLY="0")
    check("routable bind + explicit opt-out refuses to start",
          m is None and err is not None and "Refusing to start" in err)

    m, _ = load_server(DEMO_READONLY="1")
    check("read-only can be forced locally too",
          m is not None and m.HOST == "localhost" and m.DEMO_READONLY is True)

    # --- syntax floor -----------------------------------------------------
    # A nested same-quoted expression inside an f-string is PEP 701 and parses
    # only on 3.12+. It ran locally and crash-looped the deploy container on
    # 3.11 with a SyntaxError -- the whole app down, found in production logs.
    # Compiling against the OLDEST interpreter we claim to support catches that
    # class of bug here instead. ast.parse(feature_version=...) does NOT catch
    # it (the f-string tokenizer is not downgraded), so this shells out to a
    # real interpreter and skips cleanly when that version is not installed.
    floor = "3.11"
    interp = shutil.which(f"python{floor}")
    if interp:
        srcs = [str(p) for p in pathlib.Path(ROOT).rglob("*.py")
                if not any(x in p.parts for x in (".venv", "runs", "__pycache__", "out"))]
        proc = subprocess.run(
            [interp, "-c",
             "import sys,py_compile,tempfile\n"
             "bad=[]\n"
             "for f in sys.argv[1:]:\n"
             "    try: py_compile.compile(f, cfile=tempfile.mktemp(), doraise=True)\n"
             "    except py_compile.PyCompileError as e: bad.append(f'{f}: {e}')\n"
             "print('\\n'.join(bad))\n", *srcs],
            capture_output=True, text=True,
        )
        check(f"all sources parse on Python {floor} (deploy floor)",
              proc.returncode == 0 and not proc.stdout.strip(),
              proc.stdout.strip()[:200] or proc.stderr.strip()[:200])
    else:
        print(f"  SKIP  python{floor} not installed — cannot check the deploy syntax floor")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
