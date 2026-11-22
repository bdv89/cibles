@echo off
cd /d "%~dp0"
.\venv\Scripts\python run.py --db inventory.sqlite --port 8001
