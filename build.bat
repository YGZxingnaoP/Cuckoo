@echo off
chcp 65001 >nul
echo ==========================================
echo   Cuckoo 私有实时通信平台 - 完美打包脚本
echo ==========================================
echo.

:: 1. 自动安装 PyInstaller 和 Pillow
echo [1/3] 正在检查/安装打包工具及图标转换库...
.\runtime\python.exe -m pip install pyinstaller Pillow -i https://pypi.tuna.tsinghua.edu.cn/simple
echo.

:: 清理旧的打包缓存
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist Cuckoo.spec del /f Cuckoo.spec

echo [2/3] 正在打包程序 (将 Radmin 安装包注入 EXE 内部)...
echo.

:: 2. 使用 PyInstaller 打包
:: 【关键】--add-data="Radmin;Radmin"           将安装包打入 EXE 内部
:: 【关键】--add-data="runtime/vlc;vlc"         将 VLC 运行时打入 EXE 内部
:: 【关键】--hidden-import 防止语音/投屏模块在打包后报 ModuleNotFoundError
.\runtime\python.exe -m PyInstaller --noconfirm --onefile --windowed ^
    --icon=icon.png ^
    --name="Cuckoo" ^
    --add-data="icon.png;." ^
    --add-data="Radmin;Radmin" ^
    --add-data="runtime/libvlc.dll;vlc" ^
    --add-data="runtime/libvlccore.dll;vlc" ^
    --add-data="runtime/plugins;vlc/plugins" ^
    --add-data="runtime/ffmpeg.exe;." ^
    --hidden-import=soundcard ^
    --hidden-import=soundcard._soundcard ^
    --hidden-import=dxcam ^
    --hidden-import=comtypes ^
    --hidden-import=comtypes.stream ^
    --hidden-import=turbojpeg ^
    --hidden-import=cv2 ^
    --hidden-import=numpy ^
    --hidden-import=pyaudio ^
    --hidden-import=vlc ^
    --hidden-import=func.cinema ^
    --hidden-import=func.cinema.cinema_host ^
    --hidden-import=func.cinema.cinema_guest ^
    --hidden-import=func.cinema.subtitle_tool ^
    --hidden-import=func.file_transfer ^
    --hidden-import=func.file_transfer.manager ^
    --hidden-import=func.file_transfer.common ^
    --hidden-import=func.file_transfer.send_worker ^
    --hidden-import=func.file_transfer.recv_worker ^
    main.py

echo.
echo [3/3] 打包完成！
echo.

:: 3. 打开输出文件夹
if exist dist\Cuckoo.exe (
    explorer dist
    echo ==========================================
    echo 成功！请查看 dist 文件夹里的 Cuckoo.exe。
    echo (downloads 文件夹会在首次接收文件时自动创建)
    echo ==========================================
) else (
    echo 失败！请检查上方的报错信息。
)

echo.
pause
