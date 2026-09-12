@echo off
REM ==========================================
REM CSU Library 桌面版 - 打包脚本
REM 生成单文件 .exe 到 dist/ 目录
REM ==========================================

echo [1/4] 安装/更新依赖...
pip install -r requirements.txt --quiet

echo [2/4] 复制座位表 CSV 到打包目录...
if not exist "build_data" mkdir build_data
copy "*.csv" "build_data\" >nul 2>&1

echo [3/4] 打包为单文件 exe...
pyinstaller ^
    --name "CSU_Library_Seat_Reserve" ^
    --onefile ^
    --windowed ^
    --icon=NUL ^
    --add-data "build_data;." ^
    --hidden-import=pystray ^
    --hidden-import=PIL ^
    --hidden-import=Cryptodome.Cipher.AES ^
    --hidden-import=Cryptodome.Util.Padding ^
    --clean ^
    desktop_app.py

echo [4/4] 清理临时文件...
rmdir /s /q build_data 2>nul
rmdir /s /q build 2>nul
del CSU_Library_Seat_Reserve.spec 2>nul

echo.
echo ==========================================
echo 完成！生成文件: dist\CSU_Library_Seat_Reserve.exe
echo 可复制到桌面直接运行
echo ==========================================
pause