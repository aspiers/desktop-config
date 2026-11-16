<#
.SYNOPSIS
Open links in the right Chrome profile on Windows.

.DESCRIPTION
The Windows version of bin/link-router, reading the same rules file
(.config/link-router/rules.yaml in desktop-config).  A link matching no
rule brings up a picker: open it once in a chosen profile, or also save
a rule for links like it.  install.ps1 registers this script as a
browser, so it can be chosen as the default.  See docs/link-router.md.

Usage:
  link-router.ps1 URL...
  link-router.ps1 -Match URL    print the profile id the rules give URL
  link-router.ps1               open the rules file for editing

Environment:
  LINK_ROUTER_RULES    rules file (default: the copy in this checkout)
  LINK_ROUTER_CHROME   chrome.exe to open links with (default: from the registry)
  LINK_ROUTER_DRY_RUN  if set, print what would be opened instead

Needs yq (winget install MikeFarah.yq).  Written for Windows PowerShell
5.1, which is what install.ps1 runs it with.
#>
param(
    [switch]$Match,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Url = @()
)

Set-StrictMode -Version 3
$ErrorActionPreference = 'Stop'

# This checkout is not stowed on Windows, so read the rules in place:
# .local\lib\link-router\windows -> repository root.
$Rules = if ($env:LINK_ROUTER_RULES) { $env:LINK_ROUTER_RULES } else {
    Join-Path $PSScriptRoot '..\..\..\..\.config\link-router\rules.yaml'
}
$Log = if ($env:LINK_ROUTER_LOG) { $env:LINK_ROUTER_LOG } else {
    Join-Path $env:LOCALAPPDATA 'link-router\link-router.log'
}

# Nothing shows errors when Windows runs this for a link, so keep them
# where they can be found.
function Write-Log([string]$Message) {
    New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null
    Add-Content -Path $Log -Value ("{0:yyyy-MM-dd HH:mm:ss} link-router: {1}" -f (Get-Date), $Message)
}

function Read-Rules {
    if (-not (Get-Command yq -ErrorAction SilentlyContinue)) {
        throw 'yq not found; winget install MikeFarah.yq'
    }
    if (-not (Test-Path $Rules)) { throw "cannot read $Rules" }
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    $json = (& yq -o=json '.' $Rules) -join "`n"
    if ($LASTEXITCODE) { throw "yq could not read $Rules" }
    $config = $json | ConvertFrom-Json
    if (-not (Get-Member -InputObject $config -Name rules) -or $null -eq $config.rules) {
        $config | Add-Member -Force rules @()
    }
    $config
}

# The host and path of a link; the host is lower-cased, without user
# info or port.
function Split-Url([string]$Link) {
    $uri = $null
    if (-not [Uri]::TryCreate($Link, [UriKind]::Absolute, [ref]$uri)) {
        return @{ Host = ''; Path = '/' }
    }
    $path = if ($uri.AbsolutePath) { $uri.AbsolutePath } else { '/' }
    @{ Host = $uri.Host.ToLowerInvariant(); Path = $path }
}

# Whether a rule pattern matches a host and path.  Keep in step with
# matches() in bin/link-router.
function Test-Match([string]$Pattern, [string]$HostName, [string]$Path) {
    $Pattern = $Pattern.ToLowerInvariant()
    $target = if ($Pattern.Contains('/')) { $HostName + $Path } else { $HostName }
    if ($Pattern -match '[*?\[]' -or $Pattern.Contains('/')) {
        return $target -like $Pattern
    }
    # A bare domain covers its subdomains too.
    $HostName -eq $Pattern -or $HostName.EndsWith('.' + $Pattern)
}

# The profile id of the first rule matching a link, or $null.
function Find-Profile($Config, [string]$Link) {
    $parts = Split-Url $Link
    foreach ($rule in $Config.rules) {
        if ($rule.match -and (Test-Match $rule.match $parts.Host $parts.Path)) {
            return $rule.profile
        }
    }
    $null
}

function Get-ProfileDir($Config, [string]$Id) {
    $entry = $Config.profiles | Where-Object { $_.id -eq $Id } | Select-Object -First 1
    if (-not $entry) { throw "no profile '$Id' in $Rules" }
    if (Get-Member -InputObject $entry -Name dir_windows) { $entry.dir_windows } else { $entry.dir }
}

function Add-Rule([string]$Pattern, [string]$ProfileId) {
    # Windows PowerShell mangles double quotes in arguments to programs,
    # so the yq expression goes in a file and the values in variables.
    $expression = New-TemporaryFile
    try {
        Set-Content -Path $expression -Value `
            '.rules = (.rules // []) + [{"match": strenv(LR_MATCH), "profile": strenv(LR_PROFILE)}]'
        $env:LR_MATCH = $Pattern
        $env:LR_PROFILE = $ProfileId
        & yq -i --from-file $expression.FullName $Rules
        if ($LASTEXITCODE) { throw "could not add rule $Pattern -> $ProfileId to $Rules" }
    }
    finally {
        Remove-Item $expression
    }
}

# Ask which profile to open a link in, and whether to save a rule.
# Returns @{ Profile; Pattern } with Pattern empty for "just this once",
# or $null when cancelled.
function Show-Picker($Config, [string]$Link) {
    $hostName = (Split-Url $Link).Host
    if ($env:LINK_ROUTER_TEST_PICK) {
        # For test-link-router.ps1: "profile" or "profile|pattern".
        $id, $pattern = $env:LINK_ROUTER_TEST_PICK -split '\|', 2
        return @{ Profile = $id; Pattern = $pattern }
    }

    Add-Type -AssemblyName System.Windows.Forms
    $form = New-Object Windows.Forms.Form -Property @{
        Text = 'Link router'; AutoSize = $true; AutoSizeMode = 'GrowAndShrink'
        StartPosition = 'CenterScreen'; TopMost = $true
        FormBorderStyle = 'FixedDialog'; MaximizeBox = $false; MinimizeBox = $false
    }
    $layout = New-Object Windows.Forms.FlowLayoutPanel -Property @{
        FlowDirection = 'TopDown'; AutoSize = $true; Padding = 10
    }
    $layout.Controls.Add((New-Object Windows.Forms.Label -Property @{
        Text = "No rule matches $hostName. Open it in:"; AutoSize = $true
    }))

    $buttons = New-Object Windows.Forms.FlowLayoutPanel -Property @{ AutoSize = $true }
    foreach ($entry in $Config.profiles) {
        $button = New-Object Windows.Forms.Button -Property @{
            Text = $entry.label; Tag = $entry.id; AutoSize = $true
        }
        $button.Add_Click({ $form.Tag = $this.Tag; $form.Close() })
        $buttons.Controls.Add($button)
    }
    $layout.Controls.Add($buttons)

    $remember = New-Object Windows.Forms.CheckBox -Property @{
        Text = 'Always use that profile for links matching:'; AutoSize = $true
    }
    $pattern = New-Object Windows.Forms.TextBox -Property @{
        Text = ($hostName -replace '^www\.', ''); Width = 300; Enabled = $false
    }
    $remember.Add_CheckedChanged({ $pattern.Enabled = $remember.Checked })
    $layout.Controls.Add($remember)
    $layout.Controls.Add($pattern)
    $layout.Controls.Add((New-Object Windows.Forms.Label -Property @{
        Text = 'A bare domain covers its subdomains too; * and ? are wildcards.'
        AutoSize = $true; ForeColor = [Drawing.SystemColors]::GrayText
    }))

    $cancel = New-Object Windows.Forms.Button -Property @{ Text = 'Cancel'; AutoSize = $true }
    $cancel.Add_Click({ $form.Close() })
    $form.CancelButton = $cancel
    $layout.Controls.Add($cancel)

    $form.Controls.Add($layout)
    [void]$form.ShowDialog()
    if (-not $form.Tag) { return $null }
    $saved = if ($remember.Checked) { $pattern.Text.Trim() } else { '' }
    @{ Profile = $form.Tag; Pattern = $saved }
}

function Get-Chrome {
    if ($env:LINK_ROUTER_CHROME) { return $env:LINK_ROUTER_CHROME }
    foreach ($root in 'HKCU:', 'HKLM:') {
        $key = "$root\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
        if (Test-Path $key) { return (Get-ItemProperty $key).'(default)' }
    }
    'chrome.exe'
}

function Open-Link($Config, [string]$ProfileId, [string]$Link) {
    $dir = Get-ProfileDir $Config $ProfileId
    if ($env:LINK_ROUTER_DRY_RUN) {
        "open $dir $Link"
        return
    }
    Start-Process -FilePath (Get-Chrome) -ArgumentList "--profile-directory=`"$dir`"", "`"$Link`""
}

try {
    if (-not $Url -and -not $Match) {
        Start-Process notepad.exe -ArgumentList "`"$((Resolve-Path $Rules).Path)`""
        return
    }
    $config = Read-Rules
    if ($Match) {
        if ($Url.Count -ne 1) { throw 'usage: link-router.ps1 -Match URL' }
        $found = Find-Profile $config $Url[0]
        if ($found) { $found } else { exit 1 }
        return
    }
    foreach ($link in $Url) {
        $id = Find-Profile $config $link
        if (-not $id) {
            $choice = Show-Picker $config $link
            if (-not $choice) { continue }
            $id = $choice.Profile
            if ($choice.Pattern) { Add-Rule $choice.Pattern $id }
        }
        Open-Link $config $id $link
    }
}
catch {
    Write-Log "$_"
    if ($env:LINK_ROUTER_DRY_RUN) { throw }
    exit 1
}
