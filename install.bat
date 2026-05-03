@echo off
setlocal enabledelayedexpansion

echo ============================================
echo   DevSeek - Instalacao de dependencias
echo ============================================
echo.

echo Escolha o navegador para automacao:
echo.
echo   [1] Google Chrome (recomendado - melhor suporte a deteccao)
echo   [2] Microsoft Edge
echo   [3] Ambos
echo.
set /p BROWSER="Digite a opcao (1-3): "

if "%BROWSER%"=="1" goto chrome_only
if "%BROWSER%"=="2" goto edge_only
if "%BROWSER%"=="3" goto both
echo Opcao invalida. Usando Chrome por padrao...
set BROWSER=1

:chrome_only
echo.
echo [1/2] Instalando dependencias Python (Chrome)...
python -m pip install --upgrade setuptools PyQt5 undetected-chromedriver "selenium>=4.15.0" watchdog "fastapi>=0.115.0" "uvicorn>=0.32.0" "httpx>=0.28.0" >nul 2>&1
if errorlevel 1 (
    echo ERRO: Falha ao instalar pacotes. Verifique se o pip esta no PATH.
    pause
    exit /b 1
)
set BROWSER_CHOICE=chrome
goto verify

:edge_only
echo.
echo [1/2] Instalando dependencias Python (Edge)...
python -m pip install --upgrade setuptools PyQt5 "selenium>=4.15.0" watchdog "fastapi>=0.115.0" "uvicorn>=0.32.0" "httpx>=0.28.0" >nul 2>&1
if errorlevel 1 (
    echo ERRO: Falha ao instalar pacotes. Verifique se o pip esta no PATH.
    pause
    exit /b 1
)
set BROWSER_CHOICE=edge
goto verify

:both
echo.
echo [1/2] Instalando dependencias Python (Chrome + Edge)...
python -m pip install --upgrade setuptools PyQt5 undetected-chromedriver "selenium>=4.15.0" watchdog "fastapi>=0.115.0" "uvicorn>=0.32.0" "httpx>=0.28.0" >nul 2>&1
if errorlevel 1 (
    echo ERRO: Falha ao instalar pacotes. Verifique se o pip esta no PATH.
    pause
    exit /b 1
)
set BROWSER_CHOICE=both
goto verify

:verify
echo.
echo [2/2] Verificando instalacao...
python -c "import PyQt5; print('PyQt5 OK')"
python -c "import selenium; print('Selenium OK')"
python -c "import fastapi; print('FastAPI OK')"
python -c "import uvicorn; print('Uvicorn OK')"
python -c "import httpx; print('httpx OK')"

if "%BROWSER_CHOICE%"=="chrome" (
    python -c "import undetected_chromedriver; print('undetected-chromedriver OK')"
    setx DEVSEEK_BROWSER chrome >nul 2>&1
) else if "%BROWSER_CHOICE%"=="edge" (
    python -c "from selenium.webdriver.edge.options import Options; print('Selenium Edge OK')"
    setx DEVSEEK_BROWSER edge >nul 2>&1
) else (
    python -c "import undetected_chromedriver; print('undetected-chromedriver OK')"
    python -c "from selenium.webdriver.edge.options import Options; print('Selenium Edge OK')"
    setx DEVSEEK_BROWSER chrome >nul 2>&1
)

echo.
echo ============================================
echo   Instalacao concluida!
echo.

if "%BROWSER_CHOICE%"=="chrome" (
    echo   Navegador: Google Chrome
    echo   Execute: python main.py
    echo   Mobile bridge: run_mobile_bridge.bat
) else if "%BROWSER_CHOICE%"=="edge" (
    echo   Navegador: Microsoft Edge
    echo   Execute: python main.py
    echo   Mobile bridge: run_mobile_bridge.bat
) else (
    echo   Navegadores: Chrome + Edge
    echo   Execute: python main.py
    echo   Mobile bridge: run_mobile_bridge.bat
)

echo.
echo ============================================
pause
