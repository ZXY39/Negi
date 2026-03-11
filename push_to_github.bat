@echo off
cd /d d:\zxy\project\G3ku
echo Configuring Git identity for this session...
git config user.name "ZXY39"
git config user.email "1045273950@qq.com"

echo Checking status...
git status

echo Adding files...
git add .

echo Committing...
git commit -m "Initial commit by Antigravity script"

echo Branching...
git branch -M main

echo Pushing...
git push -u origin main

echo.
echo If the push failed with "Permission denied", please:
echo 1. Open Control Panel - User Accounts - Credential Manager
echo 2. Remove any "git:https://github.com" credentials
echo 3. Run this script again
echo.
pause
