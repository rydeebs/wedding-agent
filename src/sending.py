"""
Outreach delivery -- the one place an email can leave this system.

The agent does not send. It never has and it still does not: it drafts to
runs/<id>/drafts/ and stops. What lives here is the HUMAN's send button --
one venue, one explicit click, one confirmation screen showing the final
recipient, subject and body. There is no batch send, no scheduled send, no
"send all recommended", and `auto_send` remains unimplemented. Sending is
irreversible and carries the couple's name, so it stays a decision a person
makes one venue at a time.

Two rules this module exists to enforce:

  1. THE RECIPIENT COMES FROM THE VENUE RECORD, NOT FROM THE REQUEST.
     A caller may only send to the address the extractor found on that venue's
     own page (contact_value, and only when contact_method == "email"). The
     address in the request is checked against the stored record and refused if
     it differs by so much as a character. A UI bug, a stale tab, or a crafted
     POST cannot redirect a wedding inquiry to an address of its choosing.
     If the record has no email, there is nothing to send to and the UI says so
     instead of offering a button.

  2. MOCK MODE NEVER TOUCHES THE NETWORK.
     In mock mode a send writes the final text to runs/<id>/drafts/ and records
     itself as a demo send. The whole flow -- including this module -- is
     demonstrable and testable with no Gmail account, no credentials, and no
     network. Real delivery happens only in real mode with a valid OAuth token.

Gmail access is OAuth-only, scoped to gmail.send and nothing else. This module
never sees, asks for, or stores a password; the one-time consent happens in the
browser. credentials.json and token.json are read from git-ignored paths.
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from email.message import EmailMessage

from . import config

# Minimum viable scope: send only. Not gmail.compose, not gmail.modify, not
# anything that can read the user's mail. Widening this is a decision for a
# human, not a convenience.
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# Secrets live outside the code, at git-ignored paths (see .gitignore).
CREDENTIALS_PATH = os.environ.get("GMAIL_CREDENTIALS_PATH", "credentials.json")
TOKEN_PATH = os.environ.get("GMAIL_TOKEN_PATH", "token.json")

def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ledger_path() -> str:
    """Where "we have already contacted this venue" is remembered.

    Deliberately NOT inside runs/<id>/. A rerun produces a new run directory,
    and if the record of who has been emailed lived there, every rerun would
    forget and offer to email the same venue again. Contacting a real venue
    twice because the software lost track is exactly the kind of embarrassment
    this whole system is built to avoid, so the ledger outlives any single run.
    """
    return os.environ.get("SENT_LEDGER_PATH") or os.path.join(_repo_root(), "sent_log.json")

MAX_SUBJECT = 300
MAX_BODY = 20_000

_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[A-Za-z]{2,}$")


class SendRefused(Exception):
    """A send was rejected before anything left the machine."""


# --- recipient -------------------------------------------------------------

def resolve_recipient(record: dict) -> str | None:
    """The ONLY address this venue may be contacted at, or None.

    Deliberately narrow: an email contact_method with a syntactically valid
    contact_value. A phone number or a web form is not a send target, and the
    UI is expected to say "no email found -- send manually" rather than guess.
    """
    if not isinstance(record, dict):
        return None
    if record.get("contact_method") != "email":
        return None
    value = (record.get("contact_value") or "").strip()
    if not value or not _EMAIL_RE.match(value):
        return None
    return value


def validate_send(record: dict, to: str, subject: str, body: str) -> str:
    """Check a send request against the stored record. Returns the approved
    recipient, or raises SendRefused. Nothing here touches the network."""
    approved = resolve_recipient(record)
    if approved is None:
        raise SendRefused(
            "this venue record has no extracted email address; "
            "there is nothing to send to (send manually)"
        )

    submitted = (to or "").strip()
    if not submitted:
        raise SendRefused("no recipient supplied")
    if submitted.lower() != approved.lower():
        # The whole point of the rule. Say what was approved so a human can
        # see the mismatch, but do not send to the submitted address.
        raise SendRefused(
            f"recipient {submitted!r} does not match the address extracted from "
            f"this venue's page ({approved!r}). The recipient cannot be changed."
        )

    if not (subject or "").strip():
        raise SendRefused("subject is empty")
    if not (body or "").strip():
        raise SendRefused("body is empty")
    if len(subject) > MAX_SUBJECT:
        raise SendRefused(f"subject is too long (max {MAX_SUBJECT} chars)")
    if len(body) > MAX_BODY:
        raise SendRefused(f"body is too long (max {MAX_BODY} chars)")

    return approved


# --- gmail (real mode only) ------------------------------------------------

def auth_status() -> dict:
    """Is Gmail connected? Never raises -- the UI polls this."""
    status = {
        "connected": False,
        "mode": config.MODE,
        "scopes": SCOPES,
        "credentials_present": os.path.exists(CREDENTIALS_PATH),
        "token_present": os.path.exists(TOKEN_PATH),
        "reason": "",
    }

    if config.MODE == "mock":
        status["reason"] = (
            "mock mode: sends are written to runs/<id>/drafts/ and never leave "
            "the machine. Gmail is not used."
        )
        return status

    if not status["credentials_present"]:
        status["reason"] = f"no OAuth client at {CREDENTIALS_PATH}"
        return status
    if not status["token_present"]:
        status["reason"] = (
            f"no token at {TOKEN_PATH}. Authorize once: python -m src.sending --authorize"
        )
        return status

    try:
        creds = _load_credentials()
    except Exception as e:  # noqa: BLE001 - status must never raise
        status["reason"] = f"token unusable: {e}"
        return status

    if not creds or not creds.valid:
        status["reason"] = "token present but not valid; re-authorize"
        return status

    granted = set(getattr(creds, "scopes", []) or [])
    if granted and not set(SCOPES).issubset(granted):
        status["reason"] = f"token is missing the gmail.send scope (has {sorted(granted)})"
        return status

    status["connected"] = True
    status["reason"] = "gmail.send authorized"
    return status


def _load_credentials():
    """Load, and if possible refresh, the stored OAuth token."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return creds


def authorize() -> str:
    """One-time OAuth consent, in a browser. Never handles a password.

    Run manually:  python -m src.sending --authorize
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not os.path.exists(CREDENTIALS_PATH):
        raise SendRefused(
            f"no OAuth client file at {CREDENTIALS_PATH}. Create a Desktop OAuth "
            "client in Google Cloud Console, download it, and save it there. "
            "It is git-ignored."
        )
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
    os.chmod(TOKEN_PATH, 0o600)
    return TOKEN_PATH


def _gmail_send(to: str, subject: str, body: str) -> dict:
    from googleapiclient.discovery import build

    # Check auth BEFORE touching the token file. Reading it directly raised a
    # bare FileNotFoundError when no token existed, which the server surfaced as
    # a 502 "[Errno 2] No such file or directory: 'token.json'" -- true, but
    # useless to someone who just wants to know why their email did not send.
    status = auth_status()
    if not status["connected"]:
        raise SendRefused(
            f"Gmail is not connected: {status['reason']}. "
            "Set it up once with: python -m src.sending --authorize"
        )

    creds = _load_credentials()
    if not creds or not creds.valid:
        raise SendRefused("Gmail is not authorized; run python -m src.sending --authorize")

    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service = build("gmail", "v1", credentials=creds)
    sent = service.users().messages().send(userId="me", body={"raw": encoded}).execute()
    return {"gmail_message_id": sent.get("id")}


# --- the send --------------------------------------------------------------

def send(record: dict, to: str, subject: str, body: str, run_dir: str) -> dict:
    """Validate, then deliver exactly one inquiry. Called only from an explicit
    per-venue human confirmation in the UI.

    Mock mode writes the final text to the run's drafts/ directory and returns
    mode="mock". Real mode sends through Gmail. Either way the send is recorded
    in the run's sent log so the card can show "inquiry sent" with a timestamp,
    and so a second click cannot quietly send twice.
    """
    approved = validate_send(record, to, subject, body)
    url = record.get("url", "")

    already = read_sent_log().get(url)
    if already:
        raise SendRefused(
            f"an inquiry was already sent to this venue at {already['sent_at']}"
        )

    sent_at = datetime.now(timezone.utc).isoformat()

    if config.MODE == "mock":
        # No network, no Gmail, no credentials. The demo path is the default.
        path = _write_demo_send(run_dir, url, approved, subject, body)
        result = {"mode": "mock", "demo_file": path}
    else:
        result = {"mode": "real", **_gmail_send(approved, subject, body)}

    entry = {"to": approved, "subject": subject, "sent_at": sent_at,
             "run_id": os.path.basename(run_dir.rstrip("/")), **result}
    _append_sent_log(url, entry)
    return entry


def _slug(url: str) -> str:
    base = (url or "venue").rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._-]", "_", base) or "venue"


def _write_demo_send(run_dir: str, url: str, to: str, subject: str, body: str) -> str:
    outdir = os.path.join(run_dir, "drafts")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"{_slug(url)}.sent.txt")
    with open(path, "w") as f:
        f.write("MODE: mock (demo send -- not delivered, no network used)\n")
        f.write(f"TO: {to}\nSUBJECT: {subject}\n\n{body}\n")
    return path


# --- sent ledger -----------------------------------------------------------

def read_sent_log() -> dict:
    """url -> what was sent to it. Survives reruns; see ledger_path()."""
    path = ledger_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _append_sent_log(url: str, entry: dict) -> None:
    log = read_sent_log()
    log[url] = entry
    path = ledger_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(log, f, indent=2)


if __name__ == "__main__":
    import sys

    if "--authorize" in sys.argv:
        print(f"Authorizing Gmail for scope: {' '.join(SCOPES)}")
        print(f"Reading OAuth client from: {CREDENTIALS_PATH}")
        path = authorize()
        print(f"Token written to {path} (git-ignored, mode 600).")
    else:
        print(json.dumps(auth_status(), indent=2))
