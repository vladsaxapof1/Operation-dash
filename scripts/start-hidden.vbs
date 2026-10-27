' Starts Pult without a console window. Argument "open" also opens the browser.
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = root
pyw = root & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(pyw) Then
  MsgBox "Pult is not installed yet. Run INSTALL.bat first.", 48, "Pult"
  WScript.Quit 1
End If
extra = ""
If WScript.Arguments.Count > 0 Then
  If WScript.Arguments(0) = "open" Then extra = " --open"
End If
sh.Run """" & pyw & """ -m opsdash daemon" & extra, 0, False
