"""Build the static demo site (for GitHub Pages) into _site/.

The site is the normal page plus the app's Python files. The visitor's browser runs that Python with Pyodide,
so there is no server: every visitor gets their own private copy of the demo, kept in their browser.

    python scripts/build_static.py          # writes _site/
    python -m http.server -d _site 8001     # then open http://localhost:8001
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_site"

PYODIDE_VERSION = "314.0.7"
# Loaded from Pyodide's own package set (these have compiled parts).
PYODIDE_PACKAGES = ["micropip", "pydantic"]  # sqlite3 is built in
# Pure-Python packages installed in the browser with micropip. Keep in step with requirements.txt.
PIP_PACKAGES = ["fastapi", "python-multipart", "python-dotenv", "httpx"]
# Command-line helpers that are never used by the web app.
SKIP = {"check_middesk.py"}

SHIM_TAG = '<script src="static-shim.js"></script>\n'


def build(out: Path = OUT) -> Path:
    if out.exists():
        shutil.rmtree(out)
    (out / "py" / "app").mkdir(parents=True)

    files = []
    for src in sorted((ROOT / "app").glob("*.py")):
        if src.name in SKIP:
            continue
        shutil.copy2(src, out / "py" / "app" / src.name)
        files.append(f"app/{src.name}")
    shutil.copy2(ROOT / "web" / "bridge.py", out / "py" / "bridge.py")
    files.append("bridge.py")

    manifest = {
        "pyodide_version": PYODIDE_VERSION,
        "pyodide_packages": PYODIDE_PACKAGES,
        "pip_packages": PIP_PACKAGES,
        "files": files,
    }
    (out / "py" / "manifest.json").write_text(json.dumps(manifest, indent=2))

    page = (ROOT / "app" / "static" / "index.html").read_text()
    marker = "<script>"
    if marker not in page:
        sys.exit("Could not find the page's <script> tag to add the in-browser API.")
    page = page.replace(marker, SHIM_TAG + marker, 1)
    (out / "index.html").write_text(page)
    shutil.copy2(ROOT / "web" / "static-shim.js", out / "static-shim.js")
    (out / ".nojekyll").write_text("")  # serve files as-is on GitHub Pages
    return out


if __name__ == "__main__":
    print(f"Built {build()}")
