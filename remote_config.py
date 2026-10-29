import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from ollama import Client
from streamlit.errors import StreamlitSecretNotFoundError

PROJECT_DIR = Path(__file__).resolve().parent
LOCAL_OLLAMA_HOST = "http://127.0.0.1:11434"
load_dotenv(dotenv_path=PROJECT_DIR / ".env")


def get_setting(name, default=None):
    value = os.environ.get(name)
    if value:
        return value

    try:
        import streamlit as st

        value = st.secrets.get(name)
    except StreamlitSecretNotFoundError:
        value = None

    return value or default


def has_remote_ollama_host():
    host = get_setting("OLLAMA_HOST", LOCAL_OLLAMA_HOST)
    return host.rstrip("/") != LOCAL_OLLAMA_HOST.rstrip("/")


@lru_cache(maxsize=1)
def get_ollama_client():
    host = get_setting("OLLAMA_HOST", LOCAL_OLLAMA_HOST)
    api_key = get_setting("OLLAMA_API_KEY")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    if headers:
        return Client(host=host, headers=headers)
    return Client(host=host)
