' OmniSync-Downloader (OSDD) - Silent Background Launcher
Option Explicit
Dim WshShell, fso, strDir

Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
strDir = fso.GetParentFolderName(WScript.ScriptFullName)

WshShell.CurrentDirectory = strDir
' Run run.bat in completely hidden window mode (0 = Hide, False = Return immediately)
WshShell.Run "cmd.exe /c """ & strDir & "\run.bat""", 0, False
