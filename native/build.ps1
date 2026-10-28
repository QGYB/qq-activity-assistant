# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
param([Parameter(Mandatory=$true)][string]$Zig)
$ErrorActionPreference = 'Stop'
$sourceDir = $PSScriptRoot
& $Zig cc -target x86_64-windows-gnu -shared -O2 -Wall -Wextra -Werror "$sourceDir/qq_activity_boot.c" -o "$sourceDir/qq_activity_boot.dll"
if ($LASTEXITCODE -ne 0) { throw 'Native DLL compilation failed' }
Get-FileHash -Algorithm SHA256 -LiteralPath "$sourceDir/qq_activity_boot.dll"
