' ===========================================================================
'  ShortForge - launch the dashboard with NO console window ("app feel").
'  Double-click this file. It runs start_ui.bat hidden; the browser still
'  opens automatically. To stop the server later, end "python" in Task Manager.
' ===========================================================================
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir
' 0 = hidden window, False = do not wait. Extra quotes handle spaces in the path.
sh.Run "cmd /c """ & scriptDir & "\start_ui.bat""", 0, False
