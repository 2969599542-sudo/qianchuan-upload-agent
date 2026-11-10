$ErrorActionPreference = 'Stop'
$target = Join-Path $env:LOCALAPPDATA 'Programs\QianchuanAgent'
$source = Join-Path $PSScriptRoot 'QianchuanAgent'
if (-not (Test-Path -LiteralPath (Join-Path $source 'QianchuanAgent.exe'))) { throw '安装文件不完整，请完整解压ZIP后重试。' }
New-Item -ItemType Directory -Path $target -Force | Out-Null
Copy-Item -Path (Join-Path $source '*') -Destination $target -Recurse -Force
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) '千川共享盘上传助手.lnk'))
$shortcut.TargetPath = Join-Path $target 'QianchuanAgent.exe'
$shortcut.WorkingDirectory = $target
$shortcut.Save()
Write-Host '安装完成。请双击桌面上的“千川共享盘上传助手”。'
