"""One-time sign-in for the meeting bot's Google account.

Run:  python bot_login.py
A browser opens: sign in to the Google account the bot should use (a dedicated
account is recommended), then press Enter here. The session is saved to
bot_google_state.json (git-ignored) and bot_worker.py reuses it to join Google Meet.
"""
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_DIR = Path(__file__).resolve().parent
STATE_FILE = Path(os.environ.get("BOT_STATE_FILE") or PROJECT_DIR / "bot_google_state.json")


def launch(playwright):
    options = dict(
        headless=False,
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled"],
    )
    try:
        return playwright.chromium.launch(channel="chrome", **options)
    except Exception:
        return playwright.chromium.launch(**options)


def main():
    with sync_playwright() as playwright:
        browser = launch(playwright)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://accounts.google.com/signin")
        input("Sign in to Google in the opened browser, then press Enter here... ")
        context.storage_state(path=str(STATE_FILE))
        browser.close()
    print(f"Saved the bot's Google session to {STATE_FILE}")


if __name__ == "__main__":
    main()
