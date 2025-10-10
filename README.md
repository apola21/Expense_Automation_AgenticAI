# Virtual Environment (venv)

This folder contains environment-related files for the project. It's not the actual virtual environment directory created by Python's venv module; instead it stores supporting files (README, .gitignore, requirements.txt) as requested.

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
