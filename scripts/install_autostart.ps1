# Instala (ou remove) a tarefa agendada "Clipador" que inicia scripts\run_forever.bat no logon do usuário.
# Uso (PowerShell, na pasta do projeto):
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove
param([switch]$Remove)

$taskName = "Clipador"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$bat = Join-Path $root "scripts\run_forever.bat"

if ($Remove) {
    schtasks /Delete /TN $taskName /F | Out-Null
    Write-Host "Tarefa '$taskName' removida."
    exit 0
}

if (-not (Test-Path $bat)) { Write-Error "não achei $bat"; exit 1 }

# /SC ONLOGON: ao entrar no Windows; /RL LIMITED: sem admin; janela mínima via cmd /c start /min
$cmd = "cmd.exe /c start `"Clipador`" /min `"$bat`""
schtasks /Create /TN $taskName /TR $cmd /SC ONLOGON /RL LIMITED /F | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "schtasks falhou"; exit 1 }
Write-Host "Tarefa '$taskName' instalada: o Clipador sobe sozinho no próximo logon."
Write-Host "Para evitar que o PC durma com live rodando: Configurações > Sistema > Energia > Suspender: Nunca (na tomada)."
Write-Host "Para iniciar agora sem reiniciar: schtasks /Run /TN $taskName"
