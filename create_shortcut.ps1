$WshShell = New-Object -comObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Jarvis.lnk")
$Shortcut.TargetPath = "c:\Users\HP\Downloads\Jarvis\run_jarvis.bat"
$Shortcut.WorkingDirectory = "c:\Users\HP\Downloads\Jarvis"
$Shortcut.WindowStyle = 7
$Shortcut.Save()
