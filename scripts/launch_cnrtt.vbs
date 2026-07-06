Option Explicit

Dim shell, fso, pythonw, cmd, extra, i

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

pythonw = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\pythonw.exe"
If Not fso.FileExists(pythonw) Then
    pythonw = "pythonw.exe"
End If

extra = ""
For i = 0 To WScript.Arguments.Count - 1
    extra = extra & " " & Quote(WScript.Arguments.Item(i))
Next

cmd = Quote(pythonw) & " -m cnrtt" & extra
shell.CurrentDirectory = "D:\WJC\git\cnrtt"
shell.Run cmd, 1, False

Function Quote(value)
    Quote = """" & Replace(CStr(value), """", """""") & """"
End Function
