<#
.SYNOPSIS
Checks for link-router.ps1: rule matching, and the picker's results
(open once, save a rule) with the dialog replaced by canned answers.
Runs on Linux and macOS PowerShell too; needs yq.
#>

Set-StrictMode -Version 3
$ErrorActionPreference = 'Stop'

$router = Join-Path $PSScriptRoot 'link-router.ps1'
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("link-router-test-" + [guid]::NewGuid())
New-Item -ItemType Directory $tmp | Out-Null

$failures = 0
function Check([string]$Name, [string]$Want, [string]$Got) {
    if ($Want -eq $Got) {
        "ok   $Name"
    } else {
        "FAIL ${Name}: want [$Want], got [$Got]"
        $script:failures++
    }
}

$rules = Join-Path $tmp 'rules.yaml'
Set-Content -Path $rules -Value @'
# A comment that must survive rule additions.
profiles:
  - id: work
    label: Work
    dir: Default
  - id: personal
    label: Personal
    dir: Profile 1
    dir_windows: Profile 3

rules:
  - match: example.com
    profile: work
  - match: "*.YouTube.com"
    profile: personal
  - match: docs.google.com/a/example.com/*
    profile: work
  - match: "*.google.com"
    profile: personal
'@
$env:LINK_ROUTER_RULES = $rules
$env:LINK_ROUTER_LOG = Join-Path $tmp 'log'
$env:LINK_ROUTER_DRY_RUN = '1'

function Match([string]$Link) { "$(& $router -Match $Link)" }
function Route([string]$Link) { "$(& $router $Link)" }

try {
    Check 'bare domain'            'work'     (Match 'https://example.com/')
    Check 'bare domain, subdomain' 'work'     (Match 'https://app.example.com/x?y=1')
    Check 'not a suffix of a word' ''         (Match 'https://notexample.com/')
    Check 'case, port, user info'  'personal' (Match 'https://me@WWW.youtube.com:443/watch')
    Check 'glob with path'         'work'     (Match 'https://docs.google.com/a/example.com/document/d/1')
    Check 'first match wins'       'personal' (Match 'https://docs.google.com/document/d/1')
    Check 'no rule'                ''         (Match 'https://example.org/')

    Check 'matched link opens' 'open Default https://example.com/a' (Route 'https://example.com/a')
    Check 'dir_windows overrides dir' 'open Profile 3 https://www.youtube.com/' (Route 'https://www.youtube.com/')

    $env:LINK_ROUTER_TEST_PICK = 'personal'
    Check 'pick once' 'open Profile 3 https://example.org/' (Route 'https://example.org/')
    Check 'pick once adds no rule' '' (Match 'https://example.org/')

    $env:LINK_ROUTER_TEST_PICK = 'work|example.org'
    Check 'save a rule, then open' 'open Default https://www.example.org/' (Route 'https://www.example.org/')
    $env:LINK_ROUTER_TEST_PICK = $null
    Check 'saved rule matches' 'work' (Match 'https://sub.example.org/')
    Check 'comment kept' 'True' "$((Get-Content $rules)[0] -like '# A comment that must survive*')"
}
finally {
    Remove-Item -Recurse -Force $tmp
}

if ($failures) {
    "$failures failed"
    exit 1
}
'all passed'
