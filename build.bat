@echo off
REM ===========================================================
REM  RemoveBlack 一键打包脚本（Windows）
REM
REM  生成两个 exe：
REM    dist/RemoveBlack.exe       —— GUI（双击启动；也支持把图片拖到图标）
REM    dist/RemoveBlack-cli.exe   —— 纯命令行（不弹窗，便于脚本调用）
REM
REM  入口指向项目根的 run_gui.py / run_cli.py，
REM  这两个启动器使用绝对导入，避免 PyInstaller 的相对导入坑。
REM ===========================================================

setlocal EnableDelayedExpansion
cd /d %~dp0

REM ---------- 找一个真 Python（避开 Windows Store 的 0 字节存根） ----------
set "PY="
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
) do (
    if exist %%~P (
        if not defined PY set "PY=%%~P"
    )
)
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    for /f "delims=" %%I in ('where python 2^>nul') do (
        for %%S in ("%%I") do if %%~zS GTR 0 if not defined PY set "PY=%%~I"
    )
)
if not defined PY (
    echo [ERROR] 未找到可用的 Python 3 解释器（Windows Store 的 python.exe 不算）。
    echo         请安装官方 Python 或 Anaconda 后重试。
    exit /b 1
)
echo Using Python: !PY!
set "APP_VERSION=v1.4.2"

REM ---------- 关掉旧 exe，避免 PermissionError 32 ----------
taskkill /F /IM RemoveBlack.exe /T >nul 2>nul
taskkill /F /IM RemoveBlack-cli.exe /T >nul 2>nul

echo [1/3] Installing dependencies...
!PY! -m pip install --upgrade pip --no-compile
!PY! -m pip install --no-compile -r requirements.txt
!PY! -m pip install --no-compile pyinstaller

echo.
echo [2/3] Building GUI (RemoveBlack.exe)...
!PY! -m PyInstaller ^
    --noconfirm --clean ^
    --onefile --windowed ^
    --name RemoveBlack ^
    --icon assets\icon.ico ^
    --version-file version_info.txt ^
    --add-data "assets\icon.ico;assets" ^
    --hidden-import PySide6.QtCore ^
    --hidden-import PySide6.QtGui ^
    --hidden-import PySide6.QtWidgets ^
    --exclude-module PySide6.QtWebEngineCore ^
    --exclude-module PySide6.QtWebEngineWidgets ^
    --exclude-module PySide6.QtWebEngineQuick ^
    --exclude-module PySide6.QtMultimedia ^
    --exclude-module PySide6.QtMultimediaWidgets ^
    --exclude-module PySide6.QtQuick ^
    --exclude-module PySide6.QtQuick3D ^
    --exclude-module PySide6.QtQuickWidgets ^
    --exclude-module PySide6.QtQml ^
    --exclude-module PySide6.QtPdf ^
    --exclude-module PySide6.QtPdfWidgets ^
    --exclude-module PySide6.QtSql ^
    --exclude-module PySide6.Qt3DCore ^
    --exclude-module PySide6.Qt3DRender ^
    --exclude-module PySide6.QtCharts ^
    --exclude-module PySide6.QtDataVisualization ^
    --exclude-module PySide6.QtSensors ^
    --exclude-module PySide6.QtBluetooth ^
    --exclude-module PySide6.QtPositioning ^
    --exclude-module PySide6.QtSerialPort ^
    --exclude-module PySide6.QtSerialBus ^
    --exclude-module PySide6.QtTextToSpeech ^
    --exclude-module PySide6.QtNfc ^
    --exclude-module PySide6.QtRemoteObjects ^
    --exclude-module PySide6.QtScxml ^
    --exclude-module PySide6.QtTest ^
    --exclude-module PySide6.QtHelp ^
    --exclude-module PySide6.QtDesigner ^
    --exclude-module PySide6.QtUiTools ^
    --exclude-module PySide6.QtSpatialAudio ^
    run_gui.py
if errorlevel 1 (
    echo [ERROR] GUI build failed. See output above.
    exit /b 1
)
copy /Y "dist\RemoveBlack.exe" "dist\RemoveBlack-!APP_VERSION!.exe" >nul

echo.
echo [3/3] Building CLI (RemoveBlack-cli.exe)...
!PY! -m PyInstaller ^
    --noconfirm --clean ^
    --onefile --console ^
    --name RemoveBlack-cli ^
    --icon assets\icon.ico ^
    --version-file version_info.txt ^
    run_cli.py
if errorlevel 1 (
    echo [ERROR] CLI build failed. See output above.
    exit /b 1
)
copy /Y "dist\RemoveBlack-cli.exe" "dist\RemoveBlack-cli-!APP_VERSION!.exe" >nul

echo.
echo ============================================================
echo  Done. Output in:  dist\RemoveBlack.exe
echo                    dist\RemoveBlack-!APP_VERSION!.exe
echo                    dist\RemoveBlack-cli.exe
echo                    dist\RemoveBlack-cli-!APP_VERSION!.exe
echo ============================================================
endlocal
