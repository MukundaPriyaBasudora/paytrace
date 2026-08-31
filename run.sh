#!/usr/bin/env bash
# PayTrace -- one-command setup and run.
#
# Usage: bash run.sh
#
# Runs the full pipeline in order: install deps, generate synthetic data,
# deterministic matching, LLM reasoning layer, metrics + validation, then
# launches the dashboard. Stops immediately on any real error (set -e)
# rather than continuing past a broken step and producing confusing output.

set -e

echo "=== PayTrace: full pipeline run ==="
echo

echo "--- [1/6] Installing dependencies ---"
pip install -r requirements.txt
echo

echo "--- [2/6] Generating synthetic dataset ---"
cd src
python generate_data.py
echo

echo "--- [3/6] Running deterministic matcher (no API calls) ---"
python deterministic_matcher.py
echo

echo "--- [4/6] Running LLM reasoning layer (requires GEMINI_API_KEY) ---"
if [ -z "$GEMINI_API_KEY" ]; then
    echo "ERROR: GEMINI_API_KEY is not set in this shell."
    echo "  Get a free key at https://aistudio.google.com/app/apikey"
    echo "  Then run: export GEMINI_API_KEY=\"your-key-here\""
    echo "  (or \$env:GEMINI_API_KEY=\"...\" in PowerShell)"
    exit 1
fi
python llm_matcher.py
echo

echo "--- [5/6] Computing metrics + validating against ground truth ---"
python metrics.py
echo

cd ..
echo "--- [6/6] Launching dashboard ---"
echo "Opening Streamlit at http://localhost:8501 ..."
streamlit run app.py