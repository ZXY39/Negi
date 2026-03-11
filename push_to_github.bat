@echo off
cd /d d:\zxy\project\G3ku
echo Configuring Git identity...
git config user.name "ZXY39"
git config user.email "1045273950@qq.com"

echo.
echo Configuring Git Network/SSL Settings to bypass SChannel issue...
git config --local http.sslBackend openssl
git config --local http.sslVerify false
echo Unsetting proxy in case there's an old one...
git config --local --unset http.proxy 2>nul
git config --local --unset https.proxy 2>nul

echo.
echo Checking for remote 'origin'...
git remote get-url origin >nul 2>&1
if %errorlevel% neq 0 (
    echo Remote 'origin' not found. Adding it...
    git remote add origin https://github.com/ZXY39/G3KU.git
) else (
    echo Remote 'origin' exists. Ensuring URL is correct...
    git remote set-url origin https://github.com/ZXY39/G3KU.git
)

echo.
echo Adding files...
git add .
git commit -m "Initial commit by Antigravity script" >nul 2>&1

echo.
echo Pushing to GitHub...
git push -u origin main

echo.
echo If the push fails with "Permission denied", please:
echo 1. Open Control Panel - User Accounts - Credential Manager
echo 2. Remove any "git:https://github.com" credentials
echo 3. Run this script again
echo.
pause
