# Google Meet local transcription prototype

A local Python prototype that opens Google Meet for manual joining, records selected audio devices, transcribes with Faster-Whisper, optionally assigns speakers with pyannote.audio, creates cited meeting minutes with Ollama, and stores searchable meeting history in SQLite.

## Local setup

Requirements: Python 3.12, Git, and the Ollama desktop runtime.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set `MEET_URL` in `.env`. For speaker diarization, accept the [pyannote community pipeline conditions](https://huggingface.co/pyannote/speaker-diarization-community-1) and set `HF_TOKEN` in `.env`. Pull the local notes model:

```powershell
ollama pull llama3.2:3b
```

Start the local dashboard:

```powershell
streamlit run meeting_app.py
```

Use **Open Meet** to join manually, confirm participant consent, then select **Start recording**. **Stop and analyze** stops capture, diarizes speakers when configured, creates timestamped Markdown and JSON minutes, and updates `meeting_history.sqlite3`.

## Meeting history

The dashboard provides meeting details, FTS5 keyword search, and Ollama-backed questions over matched transcript and notes. The CLI is also available:

```powershell
python meeting_history.py list
python meeting_history.py search "Project X deadline"
python meeting_history.py ask "Which tasks were assigned last week?"
```

## Deployment limits

This is a **local prototype**, not a ready-to-run Streamlit Community Cloud app. Community Cloud runs on a remote server, so it cannot capture your desktop microphone/speaker devices or open Meet on your computer. The current workflow also expects a local Ollama server and a writable local SQLite database. A cloud deployment needs a separate design for user-uploaded recordings or a remote capture service, hosted model inference, secret management, and persistent database storage.

Never commit `.env`, OAuth credentials, tokens, recordings, transcripts, or meeting notes. Those paths are excluded by `.gitignore`.
