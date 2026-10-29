from pathlib import Path
import runpy

APP_PATH = Path(__file__).with_name("meeting_app.py")
runpy.run_path(str(APP_PATH))
