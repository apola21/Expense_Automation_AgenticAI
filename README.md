# Demo README for Expense Auto Project

This repo is to push the code for the agentic ai solution

Flow of steps in Solution:

1. Ingestion & Triggering
2. Intelligent Document Analysis (The "Reading Agent")
3. Data Extraction & Enrichment (The "Extraction Agent")
4. Validation & Business Logic (The "Validation Agent")
5. Human-in-the-Loop & Interaction (The "Orchestrator")
6. Final Transformation & Staging (The "Formatting Agent")
7. Securely Writing to PeopleSoft (The "Writing Agent")
8. Sending a notification to the user

Quick usage (macOS, zsh):

1. Create a virtual environment (if you haven't already):

   python3 -m venv .venv

2. Activate the virtual environment:

   source .venv/bin/activate

3. Install dependencies from the `requirements.txt` in this folder:

   pip install -r venv/requirements.txt

4. Freeze installed packages into `requirements.txt`:

   pip freeze > venv/requirements.txt

Notes:

- Replace `.venv` with your preferred venv folder name.
- If your system's Python is `python` instead of `python3`, adjust the commands accordingly.
