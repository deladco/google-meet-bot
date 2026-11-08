"""Browser recorder that streams audio to Supabase storage in 5-second chunks while recording.

Nothing is lost if the Streamlit app or the tab crashes: the worker transcribes what was saved, and shows
live captions while the recording is still going. When cloud storage isn't available the recording is kept
in the browser and returned to Python when it stops (the old behaviour).
"""
import base64
import logging
from types import SimpleNamespace

import streamlit as st

import auth_db

_HTML = """
<div class="box">
  <button id="toggle" type="button"></button>
  <span id="timer"></span>
  <div id="msg"></div>
</div>
"""

_CSS = """
.box { display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem;
       font-family: var(--st-font, sans-serif); color: var(--st-text-color); }
button { padding: 0.5rem 1rem; border-radius: 0.5rem; cursor: pointer;
         border: 1px solid var(--st-primary-color, #ff4b4b);
         background: var(--st-primary-color, #ff4b4b); color: white; font-size: 1rem; }
button.recording { background: transparent; color: var(--st-primary-color, #ff4b4b); }
button:disabled { opacity: 0.5; cursor: default; }
#msg { flex-basis: 100%; font-size: 0.9rem; }
.error { color: #d93025; }
"""

_JS = """
const sessions = new WeakMap()
const CHUNK_MS = 5000
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function pickMime() {
  for (const m of ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"]) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m
  }
  return ""
}

function toBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "")
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(blob)
  })
}

const cloudReady = (d) => !!(d && d.url && d.anonKey && d.token && d.userId)

const authHeaders = (d, extra) => ({
  apikey: d.anonKey,
  Authorization: "Bearer " + d.token,
  ...extra,
})

async function createJob(d, jobId) {
  const r = await fetch(d.url + "/rest/v1/audio_jobs", {
    method: "POST",
    headers: authHeaders(d, { "Content-Type": "application/json", Prefer: "return=minimal" }),
    body: JSON.stringify({
      id: jobId,
      user_id: d.userId,
      user_email: d.email || null,
      storage_path: d.userId + "/" + jobId,
      filename: "live-recording.webm",
      language: d.language || null,
      status: "recording",
      chunked: true,
    }),
  })
  if (!r.ok) throw new Error("could not start the cloud recording (" + r.status + ")")
}

async function putChunk(s, item) {
  const d = s.data
  const name = String(item.seq).padStart(6, "0") + ".webm"
  const r = await fetch(d.url + "/storage/v1/object/recordings/" + d.userId + "/" + s.jobId + "/" + name, {
    method: "POST",
    headers: authHeaders(d, { "Content-Type": "application/octet-stream" }),
    body: item.blob,
  })
  // A retry after a lost response finds the chunk already stored.
  if (!r.ok && r.status !== 409) {
    const text = await r.text().catch(() => "")
    if (!/duplicate|already exists/i.test(text)) throw new Error("upload failed (" + r.status + ")")
  }
}

async function pump(s, say) {
  if (s.pumping) return
  s.pumping = true
  try {
    while (s.pending.length) {
      const item = s.pending[0]
      try {
        await putChunk(s, item)
        s.pending.shift()
        s.saved += 1
        s.failures = 0
      } catch (e) {
        s.failures += 1
        say("Connection problem, the recording is kept and will upload when it is back (" + e.message + ")", true)
        await sleep(Math.min(20000, 2000 * s.failures))
        if (s.giveUp) break
      }
    }
  } finally {
    s.pumping = false
  }
}

async function finishJob(s) {
  const d = s.data
  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      const r = await fetch(d.url + "/rest/v1/audio_jobs?id=eq." + s.jobId, {
        method: "PATCH",
        headers: authHeaders(d, { "Content-Type": "application/json", Prefer: "return=minimal" }),
        body: JSON.stringify({ status: "queued", updated_at: new Date().toISOString() }),
      })
      if (r.ok) return true
    } catch (e) {}
    await sleep(2000 * (attempt + 1))
  }
  return false
}

export default function (component) {
  const { parentElement, setTriggerValue, data } = component
  const button = parentElement.querySelector("#toggle")
  const timer = parentElement.querySelector("#timer")
  const msg = parentElement.querySelector("#msg")
  if (!button) return

  let s = sessions.get(parentElement)
  if (!s) {
    s = {
      recorder: null, streams: [], ctx: null, tick: null, started: 0,
      jobId: null, seq: 0, pending: [], saved: 0, failures: 0, pumping: false, giveUp: false,
      local: [], data: null, busy: false,
    }
    sessions.set(parentElement, s)
  }
  s.data = data // the token is refreshed on every rerun of the app

  const tab = data?.source === "tab"
  const idleLabel = tab ? "Start recording tab + microphone" : "Start recording"

  const say = (text, isError) => {
    msg.textContent = text || ""
    msg.className = isError ? "error" : ""
  }

  const warnOnLeave = (e) => {
    e.preventDefault()
    e.returnValue = ""
  }

  const release = () => {
    clearInterval(s.tick)
    window.removeEventListener("beforeunload", warnOnLeave)
    s.streams.forEach((st) => st.getTracks().forEach((t) => t.stop()))
    s.streams = []
    if (s.ctx) s.ctx.close().catch(() => {})
    s.ctx = null
    timer.textContent = ""
    button.classList.remove("recording")
    button.textContent = idleLabel
  }

  const stop = () => {
    if (s.recorder && s.recorder.state !== "inactive") s.recorder.stop()
  }

  const start = async () => {
    if (s.busy) return
    s.busy = true
    say("")
    let display = null
    let mic = null
    try {
      if (tab) {
        try {
          display = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true, systemAudio: "include" })
        } catch (e) {
          say("Tab sharing was cancelled or is blocked: " + e.message, true)
          return
        }
        s.streams.push(display)
        if (display.getAudioTracks().length === 0) {
          release()
          say("No audio was shared. Tick 'Share tab audio' (one tab) or choose Entire screen and tick 'Share system audio' (all tabs and apps).", true)
          return
        }
      }
      try {
        mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
        s.streams.push(mic)
      } catch (e) {
        if (!tab) {
          say("Microphone unavailable: " + e.message, true)
          return
        }
        say("Microphone unavailable, recording tab audio only: " + e.message, true)
      }

      let stream
      if (tab) {
        const ctx = new AudioContext({ sampleRate: 16000 })
        s.ctx = ctx
        const destination = ctx.createMediaStreamDestination()
        ctx.createMediaStreamSource(new MediaStream(display.getAudioTracks())).connect(destination)
        if (mic) ctx.createMediaStreamSource(mic).connect(destination)
        stream = destination.stream
      } else {
        stream = mic
      }

      s.jobId = null
      s.seq = 0
      s.pending = []
      s.saved = 0
      s.failures = 0
      s.giveUp = false
      s.local = []
      if (cloudReady(s.data)) {
        const id = crypto.randomUUID()
        try {
          await createJob(s.data, id)
          s.jobId = id
        } catch (e) {
          say("Saving to the cloud is unavailable (" + e.message + "). The recording stays in this tab until you stop.", true)
        }
      }

      const mimeType = pickMime()
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      s.recorder = recorder
      recorder.ondataavailable = (ev) => {
        if (!ev.data || !ev.data.size) return
        if (s.jobId) {
          s.seq += 1
          s.pending.push({ seq: s.seq, blob: ev.data })
          pump(s, say)
        } else {
          s.local.push(ev.data)
        }
      }
      recorder.onstop = async () => {
        const type = recorder.mimeType || "audio/webm"
        release()
        s.saving = true
        button.disabled = true
        say("Saving the last seconds...")
        const done = () => {
          s.saving = false
          button.disabled = !s.data?.enabled
        }
        if (s.jobId) {
          const started = Date.now()
          while ((s.pending.length || s.pumping) && Date.now() - started < 120000) await sleep(300)
          s.giveUp = true
          if (s.pending.length) {
            say("Some audio could not be uploaded (" + s.pending.length + " parts). Check your connection and record again.", true)
            done()
            return
          }
          const ok = await finishJob(s)
          done()
          if (!ok) {
            say("Saved, but could not mark the recording as finished. The worker will pick it up in a few minutes.", true)
          } else {
            say("Saved. The worker is transcribing it.")
          }
          setTriggerValue("finished", { job_id: s.jobId })
          return
        }
        const blob = new Blob(s.local, { type })
        s.local = []
        done()
        if (!blob.size) {
          say("Nothing was recorded.", true)
          return
        }
        say("Uploading recording...")
        try {
          setTriggerValue("recording", { audio: await toBase64(blob), mime: type })
          say("Recording sent for transcription.")
        } catch (e) {
          say("Could not read the recording: " + e.message, true)
        }
      }
      // Ending the screen share from the browser bar also ends the recording.
      if (display) {
        display.getVideoTracks().forEach((t) => (t.onended = stop))
        display.getAudioTracks().forEach((t) => (t.onended = stop))
      }

      recorder.start(CHUNK_MS)
      window.addEventListener("beforeunload", warnOnLeave)
      s.started = Date.now()
      s.tick = setInterval(() => {
        const sec = Math.floor((Date.now() - s.started) / 1000)
        const clock = Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0")
        const waiting = s.pending.length > 2 ? " · " + s.pending.length + " parts waiting to upload" : ""
        timer.textContent = "● " + clock + (s.jobId ? " · saved to your account" : "") + waiting
      }, 500)
      button.classList.add("recording")
      button.textContent = "Stop and transcribe"
      button.disabled = false
      if (s.jobId) setTriggerValue("started", { job_id: s.jobId })
    } finally {
      s.busy = false
    }
  }

  const recording = s.recorder && s.recorder.state === "recording"
  if (!recording && !s.busy && !s.saving) {
    button.textContent = idleLabel
    button.disabled = !data?.enabled
  }
  if (!navigator.mediaDevices || (tab && !navigator.mediaDevices.getDisplayMedia)) {
    button.disabled = true
    say(tab ? "This browser cannot capture tab audio. Use Chrome or Edge on a computer." : "This browser cannot record audio.", true)
  }
  button.onclick = () => {
    const active = s.recorder && s.recorder.state === "recording"
    if (active) stop()
    else start()
  }
}
"""

_COMPONENT = st.components.v2.component(
    "meeting_live_recorder",
    html=_HTML,
    css=_CSS,
    js=_JS,
)


def live_recorder(*, source="mic", enabled=True, language=None, key="live_recorder"):
    """Draws the recorder. Returns a namespace:

    started: job id when a cloud recording just began
    finished: job id when it just ended (the worker takes it from here)
    local: (audio_bytes, mime) when cloud saving was unavailable and the recording stayed in the browser
    """
    data = {"enabled": bool(enabled), "source": source, "language": language}
    if auth_db.is_configured() and st.session_state.get("supabase_user"):
        try:
            auth_db.client_for_user()  # refreshes the token when it is about to expire
            data.update(
                url=auth_db._url().rstrip("/"),
                anonKey=auth_db._key(),
                token=st.session_state.get("access_token"),
                userId=st.session_state.get("user_id"),
                email=st.session_state.get("supabase_user"),
            )
        except Exception:
            logging.getLogger(__name__).debug("Handled error in live_recorder", exc_info=True)
            # the recorder falls back to keeping audio in the browser
    result = _COMPONENT(
        data=data,
        key=key,
        on_started_change=lambda: None,
        on_finished_change=lambda: None,
        on_recording_change=lambda: None,
    )
    local = None
    payload = result.recording
    if payload and payload.get("audio"):
        local = (base64.b64decode(payload["audio"]), payload.get("mime", "audio/webm"))
    return SimpleNamespace(
        started=(result.started or {}).get("job_id") if result.started else None,
        finished=(result.finished or {}).get("job_id") if result.finished else None,
        local=local,
    )
