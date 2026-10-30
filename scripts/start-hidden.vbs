' Starts PULSE without a console window. Argument "open" also opens the browser.
' Like KeepAlive on macOS: if PULSE crashes, it is started again in 30 seconds. A normal stop (STOP.bat, update,
' a second copy that only opens the browser) exits with code 0 and ends this script.
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = root
' UTF-8 mode, like ops.cmd: files and output in UTF-8 whatever the Windows locale
sh.Environment("PROCESS")("PYTHONUTF8") = "1"
sh.Environment("PROCESS")("PYTHONIOENCODING") = "utf-8"
pyw = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pyw) Then
  MsgBox "PULSE is not installed yet. Run INSTALL.bat first.", 48, "PULSE"
  WScript.Quit 1
End If
extra = ""
If WScript.Arguments.Count > 0 Then
  If WScript.Arguments(0) = "open" Then extra = " --open"
End If
fails = 0
Do
  started = Now
  code = sh.Run("""" & pyw & """ -m opsdash daemon" & extra, 0, True)
  If code = 0 Then Exit Do
  extra = ""
  ' crashes right after start (broken libraries) - stop after 5 in a row, the reason is in data\logs
  If DateDiff("s", started, Now) < 120 Then fails = fails + 1 Else fails = 1
  If fails >= 5 Then Exit Do
  WScript.Sleep 30000
Loop
