@echo off
chcp 65001 >nul
title Cuckoo Communication Platform
cd /d "%~dp0"
runtime\python.exe main.py
pause
