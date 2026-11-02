@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

echo ====================================
echo Start VigilOpsAgent Services
echo ====================================
echo.

REM Check uv (optional, fallback to pip)
echo [1/6] Check package manager...
where uv >nul 2>&1
if errorlevel 1 (
    echo [INFO] uv not found, will use pip
    echo [TIP] Install uv for speed: pip install uv
    set USE_UV=0
) else (
    echo [OK] uv detected
    set USE_UV=1
)
echo.

REM Configure Python version
echo [2/6] Configure Python version...
if exist .python-version (
    set /p PYTHON_VERSION=<.python-version
    echo [INFO] Current version: !PYTHON_VERSION!
    
    REM Check 3.10 (incompatible)
    echo !PYTHON_VERSION! | findstr /C:"3.10" >nul
    if not errorlevel 1 (
        echo [WARN] Python 3.10 incompatible, updating to 3.13...
        echo 3.13> .python-version
        echo [OK] Updated to Python 3.13
    )
) else (
    echo [INFO] Creating .python-version...
    echo 3.13> .python-version
)
echo.

REM Create or sync virtual environment
echo [3/6] Setup virtual environment...
if exist .venv\Scripts\python.exe (
    echo [INFO] Venv exists, checking updates...
    
    if "!USE_UV!"=="1" (
        uv sync 2>nul
        if errorlevel 1 (
            echo [WARN] uv sync failed, using pip...
            .venv\Scripts\python.exe -m pip install -e . -q
        ) else (
            echo [OK] uv sync done
        )
    ) else (
        echo [INFO] Using pip to update...
        .venv\Scripts\python.exe -m pip install -e . -q
    )
) else (
    echo [INFO] Creating new venv...
    
    if "!USE_UV!"=="1" (
        echo [INFO] Trying uv sync...
        uv sync 2>nul
        if not errorlevel 1 (
            echo [OK] uv sync done
            goto :venv_created
        )
        echo [WARN] uv sync failed, fallback to venv...
    )
    
    echo [INFO] Using python -m venv...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Venv creation failed
        echo [TIP] Please install Python 3.11+
        pause
        exit /b 1
    )
    
    echo [INFO] Installing dependencies...
    .venv\Scripts\python.exe -m pip install --upgrade pip -q
    .venv\Scripts\python.exe -m pip install -e . -q
    if errorlevel 1 (
        echo [ERROR] Install failed
        pause
        exit /b 1
    )
    echo [OK] Venv created
)

:venv_created
echo [OK] Virtual environment ready
echo.

REM Set Python command
set PYTHON_CMD=.venv\Scripts\python.exe

REM Start Docker Compose (Milvus)
echo [4/6] Start Milvus vector database...
docker ps --format "{{.Names}}" | findstr "milvus-standalone" >nul 2>&1
if not errorlevel 1 (
    echo [INFO] Milvus container already running
) else (
    docker compose -f vector-database.yml up -d
    if errorlevel 1 (
        echo [ERROR] Docker failed, please start Docker Desktop
        pause
        exit /b 1
    )
    echo [INFO] Waiting for Milvus (10s)...
    timeout /t 10 /nobreak >nul
)
echo [OK] Milvus ready
echo.

REM Start CLS MCP Server
echo [5/6] Start CLS MCP server...
start "CLS MCP Server" /min %PYTHON_CMD% mcp_servers/cls_server.py
timeout /t 2 /nobreak >nul
echo [OK] CLS MCP server started
echo.

REM Start Monitor MCP Server
echo [6/6] Start Monitor MCP server...
start "Monitor MCP Server" /min %PYTHON_CMD% mcp_servers/monitor_server.py
timeout /t 2 /nobreak >nul
echo [OK] Monitor MCP server started
echo.

REM Start FastAPI with hot reload
echo [7/8] Start FastAPI service (with hot reload)...
start "VigilOpsAgent API" %PYTHON_CMD% -m uvicorn app.main:app --reload --host 0.0.0.0 --port 9900
echo [INFO] Waiting for startup (15s)...
timeout /t 15 /nobreak >nul
echo.

REM Check service status and upload docs
echo.
echo [INFO] Checking service status...
curl -s http://localhost:9900/health >nul 2>&1
if errorlevel 1 (
    echo [WARN] Service may not be ready yet, please wait...
) else (
    echo [OK] FastAPI service running
    echo.
    
    REM Upload aiops-docs to vector database
    echo [8/8] Upload docs to vector database...
    for %%f in (aiops-docs\*.md) do (
        echo   Upload: %%~nxf
        curl -s -X POST http://localhost:9900/api/upload -F "file=@%%f" >nul 2>&1
    )
    echo [OK] Docs uploaded
)

echo.
echo ====================================
echo Services started!
echo ====================================
echo Web UI:    http://localhost:9900
echo API Docs:  http://localhost:9900/docs
echo.
echo Logs:
echo   - FastAPI: logs\app_*.log (Loguru, daily rotation)
echo   - CLS MCP: type mcp_cls.log
echo   - Monitor: type mcp_monitor.log
echo Stop:    stop-windows.bat
echo ====================================
pause
