@echo off
chcp 65001 >nul
cd /d D:\polymarket_tracker_v0.2\polymarket_tracker
call .venv\Scripts\activate.bat
python tracker_main.py
pause