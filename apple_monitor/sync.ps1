# sync.ps1 — Upload local apple_monitor folder to the Oracle Cloud VM.
# Usage: .\sync.ps1 -IP <VM_PUBLIC_IP> [-User ubuntu] [-Key ~/.ssh/id_rsa]
#
# Run from M:\Working\Apple\apple_monitor on your Windows machine.

param(
    [Parameter(Mandatory=$true)]  [string]$IP,
    [string]$User = "ubuntu",
    [string]$Key  = "$env:USERPROFILE\.ssh\id_rsa"
)

$local  = Split-Path -Parent $MyInvocation.MyCommand.Path
$remote = "${User}@${IP}:~/apple_monitor/"

Write-Host "Syncing $local → $remote ..."

# Create remote directory first
ssh -i $Key -o StrictHostKeyChecking=no "${User}@${IP}" "mkdir -p ~/apple_monitor"

# Copy all files (exclude .pyc, __pycache__, venv, cron.log)
scp -i $Key -r `
    "$local\apple_monitor.py" `
    "$local\apple_monitor_config.py" `
    "$local\token_refresh.py" `
    "$local\run_monitor.py" `
    "$local\send_catchup.py" `
    "$local\requirements.txt" `
    "$local\setup.sh" `
    "$local\google_service_account.json" `
    "${User}@${IP}:~/apple_monitor/"

Write-Host "Done. SSH in with:"
Write-Host "  ssh -i $Key ${User}@${IP}"
Write-Host "Then run:"
Write-Host "  cd ~/apple_monitor && bash setup.sh"
