"""Entry point for Streamlit Cloud.

Streamlit adds the directory of this file to sys.path, so placing it at the
repo root means all local packages (db, core, ingestion, pipelines) are
importable without any path manipulation.
"""

from app.dashboard.main import main

main()
