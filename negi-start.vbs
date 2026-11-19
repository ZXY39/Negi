' Negi hidden entry: run negi.cmd with no console window (window style 0).
' The venv -> py -> python fallback chain lives in negi.cmd; this wrapper only hides it.
Option Explicit
Dim shell, root, args, i
Set shell = CreateObject("WScript.Shell")
root = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\") - 1)
args = ""
For i = 0 To WScript.Arguments.Count - 1
  args = args & " """ & Replace(WScript.Arguments(i), """", """""") & """"
Next
shell.Run """" & root & "\negi.cmd"" " & args, 0, False