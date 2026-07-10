@echo off
cd /d "%~dp0"
call "C:\Program Files (x86)\Microsoft Visual Studio 12.0\VC\vcvarsall.bat" x86 >nul
set "XSDK=C:\Program Files (x86)\Microsoft Xbox 360 SDK"
cl /nologo /EHsc /MT /O2 /I "%XSDK%\include\win32" %1 /link /LIBPATH:"%XSDK%\lib\win32\vs2010" xgraphics.lib d3d9.lib
