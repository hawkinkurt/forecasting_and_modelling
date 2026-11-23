"""Send email through the Gmail API.

The Gmail API works over ordinary HTTPS (port 443), so it gets through networks
that block the SMTP mail ports (465 and 587).

Access uses OAuth with the narrowest Gmail permission, gmail.send: the script
can send mail as you but cannot read, search or delete anything in the mailbox.

Two files, both gitignored, live in config/:
  gmail_credentials.json  the OAuth client downloaded from Google Cloud (identifies this app)
  gmail_token.json        created when you approve access; refreshed automatically

One-off authorisation (opens a browser to approve access):

    .\\.venv\\Scripts\\python.exe -m pipeline.gmail

Daily runs never open a browser: if the token is missing or revoked, sending
fails with a message saying to run the command above.
"""
import base64

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from pipeline.db import ROOT

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
CREDENTIALS_PATH = ROOT / "config" / "gmail_credentials.json"
TOKEN_PATH = ROOT / "config" / "gmail_token.json"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
AUTHORISE_HINT = r"run .\.venv\Scripts\python.exe -m pipeline.gmail to authorise"


class GmailNotAuthorised(Exception):
    pass


def authorise():
    """Open a browser to approve access, then save the token."""
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(f"OAuth client file not found: {CREDENTIALS_PATH}")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    # Starts a temporary local web server to receive Google's reply after you approve
    creds = flow.run_local_server(port=0, open_browser=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def load_credentials():
    """Load the saved token, refreshing it if it has expired. Never opens a browser."""
    if not TOKEN_PATH.exists():
        raise GmailNotAuthorised(f"Gmail not authorised yet: {AUTHORISE_HINT}")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        # Access tokens last about an hour; the refresh token gets a new one
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise GmailNotAuthorised(f"Gmail authorisation expired or was revoked ({e}): {AUTHORISE_HINT}") from e
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        return creds
    raise GmailNotAuthorised(f"Gmail token is not usable: {AUTHORISE_HINT}")


def send(message):
    """Send an email.message.EmailMessage. Returns the Gmail message id."""
    creds = load_credentials()
    # The API takes the complete raw email, base64url-encoded
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    response = AuthorizedSession(creds).post(SEND_URL, json={"raw": raw}, timeout=60)
    response.raise_for_status()
    return response.json().get("id")


if __name__ == "__main__":
    authorise()
    print(f"Authorised. Token saved to {TOKEN_PATH}")
