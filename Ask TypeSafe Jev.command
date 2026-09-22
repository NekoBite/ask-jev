#!/bin/bash
# Double-click in Finder: starts the Ask TypeSafe Jev web UI and opens it in your browser. Close this window to stop it.
cd "$(dirname "$0")"
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 jev_ui.py
