@echo off
REM Bflow 桌面端打包（Windows）
setlocal
cd /d "%~dp0\.."

echo.
echo [1/3] 安装依赖...
python -m pip install -r requirements.txt -r requirements-desktop.txt --quiet
if errorlevel 1 (
  echo 依赖安装失败。
  exit /b 1
)

echo.
echo [2/3] 清理旧产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo [3/3] 打包...
python -m PyInstaller packaging\Bflow.spec --noconfirm
if errorlevel 1 (
  echo 打包失败。
  exit /b 1
)

echo.
echo 完成：dist\Bflow\Bflow.exe
echo 数据目录：%%APPDATA%%\Bflow
endlocal
