param(
    [string]$Project = ".",
    [string]$Host = "0.0.0.0",
    [int]$Port = 5000,
    [string]$User = ""
)

Set-Location $PSScriptRoot

python -c "import fastapi, uvicorn" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Dependencias do bridge mobile nao encontradas."
    Write-Host "Execute primeiro: install.bat"
    Write-Host "Ou: python -m pip install -r requirements.txt"
    Write-Host ""
    exit 1
}

if (-not $User) {
    $User = Read-Host "Usuario do bridge"
}

$securePassword = Read-Host "Senha do bridge" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}

$env:DEVSEEK_BRIDGE_AUTH_USER = $User
$env:DEVSEEK_BRIDGE_AUTH_PASSWORD = $plainPassword

Write-Host ""
Write-Host "============================================"
Write-Host "  DevSeek Mobile Bridge (Secure)"
Write-Host "============================================"
Write-Host ""
Write-Host "Autenticacao ativada para o usuario:" $User
Write-Host "Iniciando servidor em:"
Write-Host "  http://127.0.0.1:$Port"
Write-Host ""
Write-Host "Para acesso externo, exponha essa porta com Cloudflare Tunnel ou Tailscale."
Write-Host "Se for usar somente HTTPS publico, voce pode definir DEVSEEK_BRIDGE_SECURE_COOKIE=1."
Write-Host ""

Start-Process "http://127.0.0.1:$Port"
python bridge_server.py --project $Project --host $Host --port $Port
