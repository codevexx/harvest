# Run the Harvest agent on this PC and connect it to your account.
# Get your token from https://harvest.codevexx.com/app -> Connect a machine.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$Coordinator = "https://harvest.codevexx.com"
$Token = "PASTE_YOUR_TOKEN_HERE"   # <-- from the dashboard, shown once
$Name = $env:COMPUTERNAME

python -m worker.agent --coordinator $Coordinator --token $Token --name $Name
