"""Entry point for hosts started with `uvicorn app:app` (e.g. a Render service set up by hand).
The app itself lives in api/app.py; render.yaml starts it as `uvicorn api.app:app`."""
from api.app import app  # noqa: F401
