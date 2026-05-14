@echo off
chcp 65001 >nul
cd /d D:\polymarket_tracker_v0.2\polymarket_tracker

echo Adding files...
git add .

echo Creating commit...
git commit -m "Auto update: %date% %time%"

echo Pushing to GitHub...
git push

echo Done!
pause
