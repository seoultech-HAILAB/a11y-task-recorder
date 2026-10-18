@echo off
chcp 65001 >nul
rem 스파이크 페이지 두 개를 서로 다른 출처(127.0.0.1:8788, localhost:8789)로 띄운다.
rem 키트 폴더의 임베디드 Python을 쓴다. 키트 경로가 다르면 아래 KIT 값을 고친다.
set "KIT=C:\a11y-pilot\kit\A11yTaskRecorder-0.4.0-chrome151.0.7922.71"
set "PY=%KIT%\app\python\python.exe"
if not exist "%PY%" set "PY=python"
cd /d "%~dp0"
start "spike-top 8788" "%PY%" -m http.server 8788 --bind 127.0.0.1
start "spike-frame 8789" "%PY%" -m http.server 8789 --bind 127.0.0.1
timeout /t 2 >nul
start "" "%KIT%\app\chrome\chrome.exe" --user-data-dir="%KIT%\app\chrome-profile-spike" --no-first-run --no-default-browser-check "http://127.0.0.1:8788/top.html"
echo 두 서버 창을 닫으면 스파이크가 끝납니다.
