#!/bin/bash
# Double-click in Finder: runs every decision spec in decisions/ (or the one you drag onto it).
cd "$(dirname "$0")"
if [ $# -gt 0 ]; then specs=("$@"); else specs=(decisions/*.json); fi
for spec in "${specs[@]}"; do
  /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 jev_brainstorm.py "$spec"
done
echo
read -n 1 -s -r -p "Done. Press any key to close."
