@echo off
rem One-click launcher for the Agent Terminal Dashboard.
rem Double-click this file (or the Desktop shortcut) to start the dashboard.
title Agent Terminal Dashboard
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch_dashboard.ps1"
