# Installs BepInEx 5, builds the TFWR Bridge plugin and installs it into the Windows game folder.
# Usage: powershell -ExecutionPolicy Bypass -File bridge\install\install_windows.ps1 [-GameDir "D:\...\The Farmer Was Replaced"]
# On Windows, BepInEx loads through winhttp.dll beside the game exe, so Steam needs no launch option.
#
# Steps:
#   1. Find the game (through Steam's library list) and the .NET SDK.
#   2. Download BepInEx, check its SHA-256 and unpack it into the game (skipped when present).
#   3. Copy BepInEx\core into bridge\vendor\BepInEx\core: the build references it.
#   4. Build the plugin against the game's own DLLs.
#   5. Copy the plugin in.
param([string]$GameDir = "")

$ErrorActionPreference = "Stop"
$Tag = "v5.4.23.6"
$Zip = "BepInEx_win_x64_5.4.23.6.zip"
$Sha256 = "6c57a714a892efb4e9e1fef63d608a3391f30ec984031c465404e4ee381a0755"
$Exe = "TheFarmerWasReplaced.exe"

$Here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Plugin = Join-Path $Here "plugin\bin\Release\TfwrBridge.dll"
$Vendor = Join-Path $Here "vendor\BepInEx\core"

# 1. Find the game in any Steam library when no folder was given.
if (-not $GameDir) {
    $steam = (Get-ItemProperty -Path "HKCU:\Software\Valve\Steam" -ErrorAction SilentlyContinue).SteamPath
    if (-not $steam) { $steam = Join-Path ${env:ProgramFiles(x86)} "Steam" }
    $libs = @($steam)
    $vdf = Join-Path $steam "steamapps\libraryfolders.vdf"
    if (Test-Path $vdf) {
        $libs += Select-String -Path $vdf -Pattern '"path"\s+"([^"]+)"' |
            ForEach-Object { $_.Matches[0].Groups[1].Value -replace '\\\\', '\' }
    }
    foreach ($lib in $libs) {
        $candidate = Join-Path $lib "steamapps\common\The Farmer Was Replaced"
        if (Test-Path (Join-Path $candidate $Exe)) { $GameDir = $candidate; break }
    }
}
if (-not $GameDir -or -not (Test-Path (Join-Path $GameDir $Exe))) {
    Write-Error "Can't find $Exe. Pass -GameDir with the game folder."
}
$sdks = if (Get-Command dotnet -ErrorAction SilentlyContinue) { & dotnet --list-sdks 2>$null } else { @() }
# The dotnet host alone is not enough: the build needs an SDK, version 8 or later.
if (-not ($sdks | Where-Object { $_ -match '^(\d+)\.' -and [int]$Matches[1] -ge 8 })) {
    Write-Error "The build needs the .NET SDK 8 or later, and a runtime alone is not enough. Found SDKs: $(if ($sdks) { $sdks -join ', ' } else { 'none' }). Install one: https://dotnet.microsoft.com/download"
}

# 2. BepInEx, checksum-verified.
if (-not (Test-Path (Join-Path $GameDir "BepInEx\core"))) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("tfwr-bridge-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $zipPath = Join-Path $tmp $Zip
    Invoke-WebRequest -Uri "https://github.com/BepInEx/BepInEx/releases/download/$Tag/$Zip" -OutFile $zipPath -UseBasicParsing
    $actual = (Get-FileHash -Algorithm SHA256 $zipPath).Hash.ToLower()
    if ($actual -ne $Sha256) { Write-Error "Checksum mismatch for ${Zip}: $actual" }
    Expand-Archive -Path $zipPath -DestinationPath $GameDir -Force
    Remove-Item -Recurse -Force $tmp
    Write-Host "Installed BepInEx $Tag"
}

# 3. The build references BepInEx.dll from the vendor folder (gitignored).
New-Item -ItemType Directory -Force -Path $Vendor | Out-Null
Copy-Item (Join-Path $GameDir "BepInEx\core\*.dll") $Vendor -Force

# 4. Build.
& dotnet build (Join-Path $Here "plugin\TfwrBridge.csproj") -c Release -nologo -v q "-p:GameDir=$GameDir"
if ($LASTEXITCODE -ne 0) { Write-Error "The build failed; see the errors above." }
if (-not (Test-Path $Plugin)) { Write-Error "The build left no $Plugin" }

# 5. Install the plugin.
$dest = Join-Path $GameDir "BepInEx\plugins\TfwrBridge"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item $Plugin $dest -Force
Write-Host "Installed plugin -> $dest\TfwrBridge.dll"
Write-Host ""
Write-Host "Start the game from Steam as usual. Then run: python bridge\client\tfwr_bridge.py ping"
