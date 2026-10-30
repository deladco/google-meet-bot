from pathlib import Path

APP_PATH = Path(__file__).with_name("meeting_app.py")
# exec (not import/runpy): the page must re-run on every Streamlit rerun, and runpy raised KeyErrors in the module cache.
exec(
    compile(APP_PATH.read_text(encoding="utf-8"), str(APP_PATH), "exec"),
    {"__name__": "__main__", "__file__": str(APP_PATH)},
)
