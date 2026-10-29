from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/meetings.space.readonly"
]


def authenticate():
    credentials = None

    if Path("token.json").exists():
        credentials = Credentials.from_authorized_user_file(
            "token.json", SCOPES
        )

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            credentials = flow.run_local_server(port=0)

        Path("token.json").write_text(
            credentials.to_json(),
            encoding="utf-8"
        )

    return credentials


if __name__ == "__main__":
    credentials = authenticate()
    print("Google OAuth authentication successful!")
