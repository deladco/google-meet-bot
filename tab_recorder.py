"""Streamlit component that records a shared browser tab's audio mixed with the microphone."""
import base64

import streamlit as st

_HTML = """
<div class="box">
  <button id="toggle" type="button">Start recording tab + microphone</button>
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

export default function (component) {
  const { parentElement, setTriggerValue, data } = component
  const button = parentElement.querySelector("#toggle")
  const timer = parentElement.querySelector("#timer")
  const msg = parentElement.querySelector("#msg")
  if (!button) return

  let session = sessions.get(parentElement)
  if (!session) {
    session = { recorder: null, streams: [], ctx: null, chunks: [], tick: null, started: 0 }
    sessions.set(parentElement, session)
  }

  const say = (text, isError) => {
    msg.textContent = text || ""
    msg.className = isError ? "error" : ""
  }

  const cleanup = () => {
    clearInterval(session.tick)
    session.streams.forEach((s) => s.getTracks().forEach((t) => t.stop()))
    session.streams = []
    if (session.ctx) session.ctx.close().catch(() => {})
    session.ctx = null
    timer.textContent = ""
    button.classList.remove("recording")
    button.textContent = "Start recording tab + microphone"
    button.disabled = !data?.enabled
  }

  const stop = () => {
    if (session.recorder && session.recorder.state !== "inactive") {
      session.recorder.stop()
    }
  }

  const start = async () => {
    say("")
    let display
    let mic = null
    try {
      display = await navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: true,
        systemAudio: "include",
      })
    } catch (e) {
      say("Tab sharing was cancelled or is blocked: " + e.message, true)
      return
    }
    session.streams.push(display)
    if (display.getAudioTracks().length === 0) {
      cleanup()
      say("No audio was shared. Tick 'Share tab audio' (one tab) or choose Entire screen and tick 'Share system audio' (all tabs and apps).", true)
      return
    }
    try {
      mic = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      })
      session.streams.push(mic)
    } catch (e) {
      say("Microphone unavailable, recording tab audio only: " + e.message, true)
    }

    const ctx = new AudioContext({ sampleRate: 16000 })
    session.ctx = ctx
    const destination = ctx.createMediaStreamDestination()
    ctx.createMediaStreamSource(new MediaStream(display.getAudioTracks())).connect(destination)
    if (mic) ctx.createMediaStreamSource(mic).connect(destination)

    session.chunks = []
    const mimeType = pickMime()
    const recorder = new MediaRecorder(destination.stream, mimeType ? { mimeType } : undefined)
    session.recorder = recorder
    recorder.ondataavailable = (ev) => {
      if (ev.data && ev.data.size) session.chunks.push(ev.data)
    }
    recorder.onstop = async () => {
      const type = recorder.mimeType || "audio/webm"
      const blob = new Blob(session.chunks, { type })
      session.chunks = []
      cleanup()
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
    // Ending the share from the browser bar also ends the recording.
    display.getVideoTracks().forEach((t) => (t.onended = stop))
    display.getAudioTracks().forEach((t) => (t.onended = stop))

    recorder.start(1000)
    session.started = Date.now()
    session.tick = setInterval(() => {
      const s = Math.floor((Date.now() - session.started) / 1000)
      timer.textContent = "● " + Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0")
    }, 500)
    button.classList.add("recording")
    button.textContent = "Stop and transcribe"
    button.disabled = false
  }

  const recording = session.recorder && session.recorder.state === "recording"
  if (!recording) button.disabled = !data?.enabled
  if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
    button.disabled = true
    say("This browser cannot capture tab audio. Use Chrome or Edge on a computer.", true)
  }
  button.onclick = () => {
    const active = session.recorder && session.recorder.state === "recording"
    if (active) stop()
    else start()
  }
}
"""

_COMPONENT = st.components.v2.component(
    "meeting_tab_recorder",
    html=_HTML,
    css=_CSS,
    js=_JS,
)


def tab_recorder(*, enabled=True, key="tab_recorder"):
    """Returns (audio_bytes, mime_type) when a recording just finished, else None."""
    result = _COMPONENT(
        data={"enabled": bool(enabled)},
        key=key,
        on_recording_change=lambda: None,
    )
    payload = result.recording
    if not payload or not payload.get("audio"):
        return None
    return base64.b64decode(payload["audio"]), payload.get("mime", "audio/webm")
