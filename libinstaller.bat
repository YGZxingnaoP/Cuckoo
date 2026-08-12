@echo off
setlocal enabledelayedexpansion
title Cuckoo 项目依赖安装工具

:: ============================================================
:: 1. 设置路径（请根据你的实际目录修改）
:: ============================================================
set RUNTIME_DIR=D:\.Cuckoo\OriginCode\runtime
set PYTHON_EXE=%RUNTIME_DIR%\python.exe
set PIP_CMD=%PYTHON_EXE% -m pip

:: 检查 Python 是否存在
if not exist "%PYTHON_EXE%" (
    echo [错误] 找不到 python.exe，请检查 RUNTIME_DIR 路径是否正确。
    echo 当前设置的路径：%RUNTIME_DIR%
    pause
    exit /b 1
)
echo [OK] 找到 Python：%PYTHON_EXE%

:: ============================================================
:: 2. 检查并安装 pip（若缺失）
:: ============================================================
echo.
echo [检查] 是否已安装 pip...
%PYTHON_EXE% -c "import pip" >nul 2>&1
if errorlevel 1 (
    echo [提示] 未找到 pip，正在下载并安装...
    cd /d %RUNTIME_DIR%
    echo 正在下载 get-pip.py ...
    %PYTHON_EXE% -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', 'get-pip.py')"
    if errorlevel 1 (
        echo [错误] 下载 get-pip.py 失败，请检查网络。
        pause
        exit /b 1
    )
    echo 正在安装 pip ...
    %PYTHON_EXE% get-pip.py
    del get-pip.py
    echo [OK] pip 安装完成。
) else (
    echo [OK] pip 已存在。
)

:: ============================================================
:: 3. 启用 site-packages（修改 python._pth）
:: ============================================================
echo.
echo [检查] 是否已启用 site-packages 导入...
set PTH_FILE=%RUNTIME_DIR%\python._pth
if not exist "%PTH_FILE%" (
    echo [警告] 未找到 python._pth 文件，请确认嵌入式环境完整。
) else (
    :: 检查是否包含 "import site" 且未被注释
    findstr /C:"^import site" "%PTH_FILE%" >nul
    if errorlevel 1 (
        echo [提示] 当前未启用 import site，正在修改配置文件...
        :: 使用 PowerShell 执行文本替换（更可靠）
        powershell -Command "(Get-Content '%PTH_FILE%') -replace '#import site', 'import site' | Set-Content '%PTH_FILE%'"
        echo [OK] 已启用 import site。
    ) else (
        echo [OK] import site 已启用。
    )
)

:: ============================================================
:: 4. 设置 pip 镜像源（清华大学）
:: ============================================================
echo.
echo [配置] 设置 pip 默认镜像源为清华大学...
%PIP_CMD% config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
%PIP_CMD% config set install.trusted-host pypi.tuna.tsinghua.edu.cn
echo [OK] 镜像源配置完成。

:: ============================================================
:: 5. 安装项目依赖
:: ============================================================
echo.
echo [安装] 开始安装项目依赖库（可能需要几分钟）...
echo 依赖列表：PySide6, opencv-python, mss, pyaudio, numpy, soundcard, dxcam, comtypes, pyturbojpeg, python-vlc
%PIP_CMD% install PySide6 opencv-python mss pyaudio numpy soundcard dxcam comtypes pyturbojpeg python-vlc
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络或手动重试。
    pause
    exit /b 1
)

echo.
echo ============================================================
echo [完成] 所有依赖安装成功！
echo 你可以运行 %PYTHON_EXE% main.py 启动程序。
echo ============================================================
pause