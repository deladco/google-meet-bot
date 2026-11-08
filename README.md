# Google Meet meeting assistant

A single-host prototype for processing uploaded meeting recordings. Streamlit serves the dashboard; Faster-Whisper, pyannote.audio, Ollama, and SQLite run on the same server. Original uploads are processed from a temporary file and discarded; transcripts, speaker labels, notes, structured action items, and the SQLite history are persisted.

## Deploy on one server

Requirements: a Linux host with Docker Engine and Docker Compose, at least 8 GB RAM (more is recommended for CPU inference), and disk space for models and recordings' derived artifacts.

1. Clone this repository on the server.
2. Copy `.env.example` to `.env` and set `HF_TOKEN`. Before using diarization, accept the [pyannote community pipeline conditions](https://huggingface.co/pyannote/speaker-diarization-community-1).
3. Start the stack and pull the local Ollama model:

```bash
docker compose up -d --build
docker compose exec ollama ollama pull llama3.2:3b
```

4. Open the dashboard through an SSH tunnel:

```bash
ssh -L 8501:127.0.0.1:8501 user@your-server
```

Then browse to `http://127.0.0.1:8501`. Compose binds Streamlit to the server's loopback interface by default. For wider access, put it behind an authenticated HTTPS reverse proxy; do not expose an unauthenticated meeting archive directly to the internet.

## Processing a meeting

Upload a consented recording (`wav`, `mp3`, `m4a`, `flac`, `ogg`, `mp4`, or `webm`) in the dashboard and select **Transcribe and analyze**. Processing runs on the server. It transcribes the audio, assigns speaker labels with pyannote when `HF_TOKEN` is configured, then creates timestamped Markdown and structured JSON minutes with Ollama.

The Docker Compose volumes persist the meeting database, generated transcripts/notes, Hugging Face model cache, and Ollama model cache across container restarts. The uploaded original audio is not retained by the application.

## Streamlit Community Cloud with hosted Ollama

If hosting only the UI on Streamlit Community Cloud, configure these values under **Manage app → Settings → Secrets**:

```toml
OLLAMA_HOST = "https://ollama.example.com"
OLLAMA_API_KEY = "your-proxy-bearer-token"
HF_TOKEN = "your-hugging-face-token"
```

`OLLAMA_HOST` must be a reachable HTTPS endpoint backed by an authenticated reverse proxy or private network. Configure the proxy to forward Ollama API requests to the private Ollama service; do not expose port `11434` directly to the public internet. The app sends `OLLAMA_API_KEY` as a bearer token when present.

Streamlit Community Cloud storage is ephemeral, so its local SQLite database and generated artifacts are not durable across restarts. Use the single-host Docker Compose deployment above for persistent local storage, or replace SQLite and local files with managed database and object-storage services before relying on Cloud for meeting history.

## Meeting history

The dashboard provides meeting details, FTS5 keyword search, and Ollama-backed questions over matched transcript and notes. The CLI is also available inside the app container or local environment:

```bash
python meeting_history.py list
python meeting_history.py search "Project X deadline"
python meeting_history.py ask "Which tasks were assigned last week?"
```

## Desktop-only workflow

`controller.py` and `live_transcriber.py` remain available for local desktop capture. They use the host machine's audio devices and are not used by the remote upload dashboard.

Never commit `.env`, OAuth credentials, tokens, virtual environments, databases, recordings, transcripts, or meeting notes. Those paths are excluded by `.gitignore` and `.dockerignore`.

## Worker recording health and restart recovery

### Dashboard worker availability

Apply `supabase/migrations/20261108151435_worker_availability.sql` once in the Supabase SQL Editor, then restart the worker with the updated code. This adds the `worker_heartbeats` table. The repository's older SQL scripts are applied separately; this migration contains only the availability feature.

The dashboard refreshes worker availability every 15 seconds. Each worker sends a heartbeat every 20 seconds, independently of recording, transcription, recovery, and calendar polling. A heartbeat older than 90 seconds is treated as offline; graceful shutdown marks the worker offline immediately. The panel shows online/offline status, whether a worker is free or busy, current activity, and the latest heartbeat. An offline warning means scheduled meetings cannot be joined until a worker starts. Busy workers continue to queue work.

The shared heartbeat contains only an opaque worker ID, a timestamp, and generic activity. It exposes no meeting URLs, job IDs, user identities, or host names. Signed-in users can read status; only the worker's service role can write it. If the migration is missing or the status query fails, the dashboard reports availability as unknown rather than claiming the worker is offline.

The meeting worker checks capture health every few seconds. It warns in the dashboard after 60 seconds of near-silence and clears the warning when sound returns. Silence alone does not end the meeting. If capture stops producing data for 20 seconds, the capture process fails, or the browser disconnects, the worker stops recording and processes the captured portion. The completed job retains a partial-recording warning.

Bot recordings and job checkpoints now live under `output/worker_recovery`, configurable with `BOT_RECOVERY_DIR`. Keep that directory, the meeting SQLite database (`MEETING_DB_PATH`), and generated transcripts/notes on persistent storage. For a container worker, mount persistent volumes at `/app/output` and `/app/data`, set `MEETING_DB_PATH=/app/data/meeting_history.sqlite3`, and keep the same volumes when replacing the container. Deleting those volumes loses local recovery data.

On startup, the worker recovers its checkpointed jobs. It repairs incomplete WAV headers and transcribes captured audio, retries interrupted mobile/browser-upload processing from cloud storage, and retries cloud saving of an already processed meeting without retranscribing it. It never automatically rejoins an interrupted call; audio during worker downtime cannot be recovered. A job interrupted before capturing audio fails with instructions to send the bot again. Jobs created before checkpoints were introduced cannot be recovered automatically.

Recovery retries are limited to three worker starts. Recordings and checkpoints are deleted after successful cloud saving; failures retain them for recovery. After the retry limit, inspect the job's `.json` and `.wav` files in the recovery directory for manual recovery. Recovery directories belong to one worker host; an OS lock prevents two local worker processes from using the same directory simultaneously. This is single-host recovery, not a distributed worker lease.

## Transcription quality

On an NVIDIA GPU the worker uses Whisper large-v3-turbo (CPU hosts: small), detects the language per segment, cleans the audio (high-pass, plus noise reduction if `noisereduce` is installed), biases Whisper toward your vocabulary, and runs an Ollama pass that fixes obvious misspellings. Tune it with the optional variables in `.env.example` (`WHISPER_MODEL`, `WHISPER_LANGUAGE`, `WHISPER_VOCABULARY` / `vocabulary.txt`, `AUDIO_CLEANUP`, `TRANSCRIPT_LLM_CORRECTION`). GPU hosts also need `nvidia-cublas-cu12 nvidia-cudnn-cu12` and a CUDA build of torch for fast speaker labelling.

Vocabulary is learned automatically: after each meeting, names and terms found in the transcript become candidates (`vocabulary_candidates.json`) and are promoted to `vocabulary_learned.txt` once they appear in two different meetings. Edit that file freely, or move good terms into `vocabulary.txt`.

## App layout

The Streamlit app has five pages: **Dashboard** (metrics, **Projects**, recent meetings, quick question, and the bot/record/upload flow), **My Meetings** (searchable history with minutes, transcript and action items), **Ask AI** (questions across your transcripts, needs `supabase_migration_rag.sql`), **Task board** (action items with done state, due dates, projects, owner/due/meeting filters, links back to the meeting and CSV export). Meeting minutes show source timestamps and can be edited and approved; edits, approval and task state are stored inside the meeting's `minutes` JSON (no migration) and work for cloud-saved meetings only and **Settings** (account, transcription language, vocabulary, Ollama connection).
