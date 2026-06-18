#!/bin/bash
# Double-click this file to open the Dalux Work Orders app.
# It moves into the app's folder and launches it.
cd "$(dirname "$0")" || exit 1
python3 workorder_app.py
