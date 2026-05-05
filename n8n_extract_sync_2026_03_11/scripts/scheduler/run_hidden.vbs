' Hidden launcher for scheduled PowerShell tasks.
' Usage (from Task Scheduler action):
'   Program/script: wscript.exe
'   Arguments:      "C:\path\to\run_hidden.vbs" "C:\path\to\target.ps1" [extra ps1 args...]
'
' wscript.exe has no console, so the spawned powershell.exe inherits no
' console window and produces no terminal pop-up flash.

Option Explicit

Dim shell, cmd, i
Set shell = CreateObject("WScript.Shell")

If WScript.Arguments.Count < 1 Then
    WScript.Quit 1
End If

cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & WScript.Arguments(0) & """"

For i = 1 To WScript.Arguments.Count - 1
    cmd = cmd & " " & WScript.Arguments(i)
Next

' 0 = hidden window, True = wait for completion so the task records the exit code.
WScript.Quit shell.Run(cmd, 0, True)
