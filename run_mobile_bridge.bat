@echo off
cd /d "%~dp0"

python -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo.
    echo Dependencias do bridge mobile nao encontradas.
    echo Execute primeiro: install.bat
    echo Ou: python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo ============================================
echo   DevSeek Mobile Bridge
echo ============================================
echo.
echo Iniciando servidor em:
echo   http://127.0.0.1:5000
echo.
echo Abra essa URL no navegador do PC ou pelo Tailscale/Cloudflare Tunnel no celular.
echo Nao abra o arquivo web\mobile\index.html diretamente.
echo.

start "" http://127.0.0.1:5000
python bridge_server.py --project . --host 0.0.0.0 --port 5000

if errorlevel 1 (
    echo.
    echo Erro ao iniciar o bridge mobile.
    echo Verifique se as dependencias estao instaladas e se a porta 5000 esta livre.
    pause
)
