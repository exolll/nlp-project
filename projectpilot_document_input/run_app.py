"""PyCharm-friendly launcher for the ProjectPilot Streamlit test page."""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.web import cli as streamlit_cli


def main() -> int:
    app_path = Path(__file__).resolve().with_name("app.py")
    sys.argv = [
        "streamlit",
        "run",
        str(app_path),
        "--browser.gatherUsageStats=false",
    ]
    return streamlit_cli.main()


if __name__ == "__main__":
    raise SystemExit(main())


