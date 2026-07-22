@echo off
chcp 65001 >nul
echo ==========================================
echo   Cuckoo 私有实时通信平台 - 完美打包脚本
echo ==========================================
echo.

:: 1. 自动安装 PyInstaller 和 Pillow (Pillow 用于自动将 PNG 转为 ICO)
echo [1/4] 正在检查/安装打包工具及图标转换库 (Pillow)...
.\runtime\python.exe -m pip install pyinstaller Pillow -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.

:: 清理旧的打包缓存
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist Cuckoo.spec del /f Cuckoo.spec

echo [2/4] 正在打包程序 (正在将 PNG 转换为 EXE 图标，请耐心等待)...
echo.

:: 2. 使用 icon.png 作为图标，PyInstaller 会调用 Pillow 自动转换
.\runtime\python.exe -m PyInstaller --noconfirm --onefile --windowed ^
    --icon=icon.png ^
    --name="Cuckoo" ^
    --add-data="icon.png;." ^
    --add-data="Radmin;Radmin" ^
    main.py

echo.
echo [3/4] 打包完成！
echo.

:: 3. 打开输出文件夹
if exist dist\Cuckoo.exe (
    explorer dist
    echo ==========================================
    echo 成功！请查看 dist 文件夹里的 Cuckoo.exe。
    echo ==========================================
) else (
    echo 失败！请检查上方的报错信息。
)

echo.
pause
