# Instala (ou remove) a tarefa agendada "Clipador" que inicia scripts\run_forever.bat no logon do usuário.
# Uso (PowerShell, na pasta do projeto):
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Remove
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -StartNow
param([switch]$Remove, [switch]$StartNow)

$taskName = "Clipador"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$bat = Join-Path $root "scripts\run_forever.bat"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Tarefa '$taskName' removida."
    exit 0
}

if (-not (Test-Path $bat)) { Write-Error "não achei $bat"; exit 1 }

# cmd /c start "Clipador" /min "<bat>"  -> janela minimizada; sem admin; só no logon deste usuário
# janela oculta: uma janela minimizada era fechada sem querer e derrubava o serviço inteiro (18/11)
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ('-NoProfile -WindowStyle Hidden -Command "& ''' + $bat + '''"') -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Tarefa '$taskName' instalada: o Clipador sobe sozinho no próximo logon."
Write-Host "Para evitar que o PC durma com live rodando: Configurações > Sistema > Energia > Suspender: Nunca (na tomada)."
if ($StartNow) {
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Tarefa iniciada agora (janela minimizada 'Clipador')."
} else {
    Write-Host "Para iniciar agora sem reiniciar: Start-ScheduledTask -TaskName $taskName"
}
