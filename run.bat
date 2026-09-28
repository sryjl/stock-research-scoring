@echo off
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"

if not defined PY (
    echo [错误] 未找到 Python，请先安装 Python 3.8 或更高版本。
    pause
    exit /b 1
)

echo 正在启动服务，稍后会自动打开浏览器 http://127.0.0.1:8765
echo 按 Ctrl+C 或关闭本窗口即可停止。
echo.
start "" /b cmd /c "timeout /t 2 >nul && start http://127.0.0.1:8765"
%PY% app.py
pause
