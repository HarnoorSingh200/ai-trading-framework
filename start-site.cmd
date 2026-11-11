@echo off
REM ---------------------------------------------------------------------------
REM  The operator site, started quietly by start-module-1.cmd.
REM
REM  It used to get a console window of its own, which meant two windows for
REM  one framework - and every proxy retry printed there in red while the
REM  framework was still coming up. Its output goes to logs/site.process.log
REM  instead: the one window the operator watches is the runner's.
REM
REM  Run by hand only if you want the site without the framework; the launcher
REM  reuses it when port 5200 is already answering.
REM ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0ui"
if not exist "%~dp0logs" mkdir "%~dp0logs"
npm run dev >> "%~dp0logs\site.process.log" 2>&1
