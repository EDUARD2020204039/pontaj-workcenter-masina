@echo off
setlocal
start /wait "" "%~dp0vc_redist.x64.exe" /install /quiet /norestart
msiexec.exe /i "%~dp0msodbcsql.msi" IACCEPTMSODBCSQLLICENSETERMS=YES /qn /norestart
exit /b %errorlevel%
