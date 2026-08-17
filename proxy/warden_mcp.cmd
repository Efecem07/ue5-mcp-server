@echo off
REM Launches the stdio bridge using Unreal's bundled Python.
REM Adjust UE_PYTHON if your engine is installed elsewhere.

set "UE_PYTHON=C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\ThirdParty\Python3\Win64\python.exe"

"%UE_PYTHON%" "%~dp0warden_mcp_proxy.py"
