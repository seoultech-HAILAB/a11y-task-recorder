# 0.5.9: restart the participant's running NVDA with its own program, so the new add-on loads
# under the same installation. Restarting through the NVDA shortcut brought up an NVDA with other
# settings (welcome dialog, Windows 10/29, 2 of 2).
# NVDA runs with UIAccess, so an ordinary PowerShell cannot read its path or command line from
# Win32_Process (both came back empty, Windows 10/29). The path is read with the limited query
# right Windows grants for this; the arguments are kept only when they can be read.
Add-Type -Namespace Kit -Name Native -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)] public static extern IntPtr OpenProcess(uint access, bool inherit, int id);
[DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] public static extern bool QueryFullProcessImageName(IntPtr process, int flags, System.Text.StringBuilder name, ref int size);
[DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
'@
function Get-ImagePath([int]$id) {
  $handle = [Kit.Native]::OpenProcess(0x1000, $false, $id)  # PROCESS_QUERY_LIMITED_INFORMATION
  if ($handle -eq [IntPtr]::Zero) { return '' }
  try {
    $name = New-Object System.Text.StringBuilder 1024
    $size = $name.Capacity
    if ([Kit.Native]::QueryFullProcessImageName($handle, 0, $name, [ref]$size)) { return $name.ToString() }
    return ''
  } finally { [void][Kit.Native]::CloseHandle($handle) }
}
$session = (Get-Process -Id $PID).SessionId
$nvda = Get-Process -Name nvda -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $session } | Select-Object -First 1
if (-not $nvda) { exit 1 }
$exe = Get-ImagePath $nvda.Id
$info = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $nvda.Id) -ErrorAction SilentlyContinue
if (-not $exe -and $info) { $exe = [string]$info.ExecutablePath }
if (-not $exe) { exit 1 }
$command = if ($info) { [string]$info.CommandLine } else { '' }
$arguments = ''
if ($command.StartsWith('"')) {
  $end = $command.IndexOf('"', 1)
  if ($end -gt 0) { $arguments = $command.Substring($end + 1) }
} elseif ($command) {
  $space = $command.IndexOf(' ')
  if ($space -gt 0) { $arguments = $command.Substring($space + 1) }
}
$arguments = $arguments.Trim()
# A second nvda.exe asks the running one to exit and takes its place (NVDA's own replace).
if ($arguments) { Start-Process -FilePath $exe -ArgumentList $arguments }
else { Start-Process -FilePath $exe }
exit 0
