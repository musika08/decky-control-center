# Renders the README images from .github\assets\src\*.html with headless Edge at 2x (sharp on high-DPI screens),
# with a transparent background so the rounded corners sit cleanly on GitHub's light and dark themes.
# Each page sets its own size in its <body>; the sizes below must match.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
# panels.html frames src\panel-top.png and src\panel-bottom.png: real captures of the Control Center panel taken from Steam
# (CDP Page.captureScreenshot of the QuickAccess view while it is on screen), cropped to the panel's width, with the
# Wi-Fi network and Bluetooth device names pixelated (panels.html also blurs over them).
$pages = @{ 'banner' = '1280,400'; 'how-it-works' = '1280,560'; 'panels' = '1280,1060' }
foreach ($name in $pages.Keys) {
    $src = Join-Path $root ".github\assets\src\$name.html"
    $out = Join-Path $root ".github\assets\$name.png"
    if (Test-Path $out) { Remove-Item $out }
    $profile = Join-Path ([IO.Path]::GetTempPath()) ('control-center-edge-' + [guid]::NewGuid())
    # Started as its own process: Edge writes harmless warnings to stderr, which Windows PowerShell 5.1 would turn
    # into terminating errors under ErrorActionPreference Stop.
    $edgeArgs = @('--headless=new', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=2',
        '--default-background-color=00000000', "`"--user-data-dir=$profile`"", "--window-size=$($pages[$name])",
        "`"--screenshot=$out`"", ([Uri]$src).AbsoluteUri)
    Start-Process -FilePath $edge -ArgumentList $edgeArgs -Wait -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(30)
    while (-not (Test-Path $out) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }
    Remove-Item $profile -Recurse -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $out)) { throw "Edge did not write $out" }
    Add-Type -AssemblyName System.Drawing
    $img = [System.Drawing.Image]::FromFile($out)
    "{0}: {1}x{2}, {3:N0} KB" -f (Split-Path -Leaf $out), $img.Width, $img.Height, ((Get-Item $out).Length / 1KB)
    $img.Dispose()
}
