<#
.SYNOPSIS
Register link-router.ps1 as a browser for the current user, so that it
can be chosen as the default for http and https links.

.DESCRIPTION
Run from this checkout of desktop-config; the registration points at
link-router.ps1 here, which reads the rules in a desktop-config-private
checkout beside it.
Installs yq with winget if needed, then opens Settings at Default apps,
where Windows requires the default browser to be chosen by hand.
See docs/link-router.md.

Usage: install.ps1 [-Uninstall]
#>
param([switch]$Uninstall)

Set-StrictMode -Version 3
$ErrorActionPreference = 'Stop'

$name = 'LinkRouter'
$client = "HKCU:\Software\Clients\StartMenuInternet\$name"
$progId = "HKCU:\Software\Classes\${name}URL"
$registered = 'HKCU:\Software\RegisteredApplications'

if ($Uninstall) {
    foreach ($key in $client, $progId) {
        if (Test-Path $key) { Remove-Item -Recurse $key }
    }
    Remove-ItemProperty -Path $registered -Name $name -ErrorAction SilentlyContinue
    'Removed.  Choose another default browser in Settings > Apps > Default apps.'
    return
}

if (-not (Get-Command yq -ErrorAction SilentlyContinue)) {
    winget install --exact --id MikeFarah.yq --source winget
}

$router = (Resolve-Path (Join-Path $PSScriptRoot 'link-router.ps1')).Path
$powershell = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$router`""
# Windows 11's console host can run without a window at all; elsewhere
# a console window flashes up briefly for each link.
if ([Environment]::OSVersion.Version.Build -ge 22000) {
    $powershell = "conhost.exe --headless $powershell"
}

function Set-Key([string]$Path, [hashtable]$Values) {
    if (-not (Test-Path $Path)) { New-Item -Path $Path -Force | Out-Null }
    foreach ($entry in $Values.GetEnumerator()) {
        Set-ItemProperty -Path $Path -Name $entry.Key -Value $entry.Value
    }
}

# What Windows runs for a link.
Set-Key $progId @{ '(default)' = 'Link Router URL'; 'URL Protocol' = '' }
Set-Key "$progId\shell\open\command" @{ '(default)' = "$powershell `"%1`"" }

# The browser entry that Default apps lists.  Starting it with no link
# opens the rules for editing.
Set-Key $client @{ '(default)' = 'Link Router' }
Set-Key "$client\shell\open\command" @{ '(default)' = $powershell }
Set-Key "$client\Capabilities" @{
    ApplicationName        = 'Link Router'
    ApplicationDescription = 'Opens links in the right Chrome profile'
}
Set-Key "$client\Capabilities\URLAssociations" @{ http = "${name}URL"; https = "${name}URL" }
Set-Key $registered @{ $name = "Software\Clients\StartMenuInternet\$name\Capabilities" }

"Registered $router as Link Router."
'In Default apps, choose Link Router and set it as the default.'
Start-Process "ms-settings:defaultapps?registeredAppUser=$name"
