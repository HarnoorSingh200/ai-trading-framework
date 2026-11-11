@echo off
REM ---------------------------------------------------------------------------
REM  Double-click this file on the operator's machine.
REM
REM  The launcher's logic lives in start-framework.ps1: batch labels and nested
REM  blocks are fragile (a .cmd saved with Unix line endings loses its labels
REM  and the window closes before printing anything), and this has to work the
REM  first time. This file is only the door to it.
REM ---------------------------------------------------------------------------

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-framework.ps1"
exit /b %errorlevel%
