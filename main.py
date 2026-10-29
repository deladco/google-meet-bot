import asyncio
import os

from playwright.async_api import async_playwright
from dotenv import load_dotenv
from transcript import wait_for_transcript

load_dotenv()

MEET_URL = os.environ["MEET_URL"]


async def join_meeting():
	async with async_playwright() as p:

		browser = await p.chromium.launch_persistent_context(
			user_data_dir="./bot_profile",
			headless=False,
			permissions=["microphone", "camera"],
			viewport={"width": 1280, "height": 800}
		)

		page = browser.pages[0] if browser.pages else await browser.new_page()

		await page.goto(MEET_URL)

		print("Google Meet opened.")

		# Complete Google sign-in manually on first run.
		# Allow the pre-meeting screen to appear.
		input(
			"Sign in if necessary, then press Enter "
			"when the meeting preview is visible..."
		)

		# Attempt to turn camera and microphone off.
		# These selectors can change when Meet updates.
		for label in [
			"Turn off microphone",
			"Turn off camera"
		]:
			control = page.get_by_role(
				"button", name=label
			)

			if await control.count() > 0:
				await control.first.click()

		join_button = page.get_by_role(
			"button",
			name="Join now",
			exact=True
		)

		ask_button = page.get_by_role(
			"button",
			name="Ask to join",
			exact=True
		)

		if await join_button.count() > 0:
			await join_button.click()
			print("Join requested.")

		elif await ask_button.count() > 0:
			await ask_button.click()
			print("Waiting for host admission.")

		else:
			print(
				"Join button not found. "
				"Check the browser manually."
			)

		print("Browser session active.")
		input("Press Enter after the meeting has ended to close the bot...")

		await browser.close()

	print("Waiting for the post-meeting transcript...")
	transcript_file = wait_for_transcript(
		timeout=int(os.environ.get("TRANSCRIPT_TIMEOUT_SECONDS", "900")),
		poll_interval=int(os.environ.get("TRANSCRIPT_POLL_SECONDS", "30"))
	)
	print(f"Transcript saved: {transcript_file}")


if __name__ == "__main__":
	asyncio.run(join_meeting())
