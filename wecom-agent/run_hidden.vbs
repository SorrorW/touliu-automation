' run_hidden.vbs - launch run_poll.bat with NO console window.
' Pure ASCII on purpose; path resolved from this script's own location.
Dim fso, sh, dir
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
sh.Run "cmd /c """ & dir & "\run_poll.bat""", 0, False
