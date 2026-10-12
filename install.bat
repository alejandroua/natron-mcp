@echo off
REM natron-mcp installer shim (Windows): double-click or run from a terminal.
REM Requires any Python 3.8+ on PATH (only for the installer itself; the MCP
REM server venv needs >= 3.10).
setlocal
set "SCRIPT=%~dp0install.py"
where py >nul 2>nul && (py -3 "%SCRIPT%" %* & goto :eof)
where python >nul 2>nul && (python "%SCRIPT%" %* & goto :eof)
echo [x] Python 3 not found on PATH. Install Python or run: py install.py %*
exit /b 1
