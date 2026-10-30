# Meet AI! — iOS app (Expo / React Native)

Phone app for the Meet AI! backend (Supabase + the worker in the repo root). Tabs: **My Meetings**, **Record**, **Ask AI**, **Action Items**, **Settings** (sign out, delete account).

Recording flow: the phone uploads audio to the private Supabase bucket `recordings` and adds a row to `audio_jobs`. `bot_worker.py` (running on your GPU machine) picks it up, transcribes it, saves the meeting to `synced_meetings` and deletes the audio.

## One-time backend setup

1. Run `supabase_migration_mobile_uploads.sql` in the Supabase SQL Editor (table, private bucket, policies, and the `delete_my_account` function that App Review requires).
2. Keep `python bot_worker.py` running on the worker machine.

## Run it

```
cd mobile
copy .env.example .env      # fill in the Supabase URL + publishable key (+ privacy policy URL)
npm install
npx expo start              # scan the QR code with Expo Go on an iPhone
```

Recording needs a real device; the simulator has no microphone.

## Publish to the App Store (needs a Mac **or** Expo cloud builds, and a paid Apple Developer account)

1. Join the Apple Developer Program ($99/year) and create the app in App Store Connect.
2. In `app.json` replace `com.example.meetai` with your own bundle identifier.
3. `npm i -g eas-cli`, then `eas login`, `eas build:configure`.
4. `eas build --platform ios --profile production` (EAS creates the certificates; no Mac needed).
5. `eas submit --platform ios` uploads the build to App Store Connect / TestFlight.
6. In App Store Connect add screenshots, description, a **privacy policy URL** (also set `EXPO_PUBLIC_PRIVACY_URL`), a demo account for the reviewers, and the privacy "nutrition label" (audio recordings and email address, linked to the user, used for app functionality).
7. Submit for review.

## Review notes

- Recording asks for consent in the app before the microphone is used, and the microphone purpose string is set in `app.json`.
- Account creation is paired with in-app account deletion (Settings), as guideline 5.1.1(v) requires.
- Ask AI currently shows the matching passages from your meetings. A written answer needs an LLM call, which should run on the server (not in the app) so no API key ships in the app.
