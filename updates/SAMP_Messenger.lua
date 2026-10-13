script_name('SAMP Messenger')
script_author('Flame_Small | SAMP Messenger project')
script_version('1.1.9')

local hasImgui, imgui = pcall(require, 'mimgui')
local ffi = require 'ffi'
-- Embedded JSON codec: no cjson.dll required.
local json = {}
local function jsonEscape(s)
  return '"' .. tostring(s):gsub('[%z\1-\31\\"]', function(c)
    local t = {['"']='\\"', ['\\']='\\\\', ['\n']='\\n', ['\r']='\\r', ['\t']='\\t', ['\b']='\\b', ['\f']='\\f'}
    return t[c] or string.format('\\u%04x', c:byte())
  end) .. '"'
end
function json.encode(v)
  local ty=type(v)
  if ty=='nil' then return 'null' end
  if ty=='boolean' then return tostring(v) end
  if ty=='number' then if v~=v or v==math.huge or v==-math.huge then error('non-finite') end; return tostring(v) end
  if ty=='string' then return jsonEscape(v) end
  if ty~='table' then error('JSON unsupported type: '..ty) end
  local array,n=true,0
  for k in pairs(v) do
    if type(k)~='number' or k<1 or k%1~=0 then array=false; break end
    if k>n then n=k end
  end
  local out={}
  if array then
    for i=1,n do out[#out+1]=json.encode(v[i]) end
    return '['..table.concat(out,',')..']'
  end
  for k,val in pairs(v) do out[#out+1]=jsonEscape(k)..':'..json.encode(val) end
  return '{'..table.concat(out,',')..'}'
end
local function utf8cp(cp)
  if cp<0x80 then return string.char(cp) end
  if cp<0x800 then return string.char(0xc0+math.floor(cp/64),0x80+cp%64) end
  if cp<0x10000 then return string.char(0xe0+math.floor(cp/4096),0x80+math.floor(cp/64)%64,0x80+cp%64) end
  return string.char(0xf0+math.floor(cp/262144),0x80+math.floor(cp/4096)%64,0x80+math.floor(cp/64)%64,0x80+cp%64)
end
function json.decode(s)
  local i,len=1,#s
  local function ws() while i<=len and s:sub(i,i):match('%s') do i=i+1 end end
  local parse
  local function str()
    i=i+1
    local out={}
    while i<=len do
      local c=s:sub(i,i)
      if c=='"' then i=i+1; return table.concat(out) end
      if c=='\\' then
        local e=s:sub(i+1,i+1)
        local special={['"']='"',['\\']='\\',['/']='/',b='\b',f='\f',n='\n',r='\r',t='\t'}
        if e=='u' then
          local cp=tonumber(s:sub(i+2,i+5),16)
          if not cp then error('Invalid unicode escape') end
          i=i+6
          if cp>=0xD800 and cp<=0xDBFF and s:sub(i,i+1)=='\\u' then
            local low=tonumber(s:sub(i+2,i+5),16)
            if low and low>=0xDC00 and low<=0xDFFF then cp=0x10000+(cp-0xD800)*1024+(low-0xDC00); i=i+6 end
          end
          out[#out+1]=utf8cp(cp)
        else
          if not special[e] then error('Invalid escape') end
          out[#out+1]=special[e]; i=i+2
        end
      else
        out[#out+1]=c; i=i+1
      end
    end
    error('Unclosed string')
  end
  function parse()
    ws()
    local c=s:sub(i,i)
    if c=='"' then return str() end
    if c=='{' then
      i=i+1; local obj={}; ws()
      if s:sub(i,i)=='}' then i=i+1; return obj end
      while true do
        ws(); if s:sub(i,i)~='"' then error('Expected object key') end
        local k=str(); ws(); if s:sub(i,i)~=':' then error('Expected colon') end
        i=i+1; obj[k]=parse(); ws()
        if s:sub(i,i)=='}' then i=i+1; return obj end
        if s:sub(i,i)~=',' then error('Expected comma') end
        i=i+1
      end
    end
    if c=='[' then
      i=i+1; local arr={}; ws()
      if s:sub(i,i)==']' then i=i+1; return arr end
      while true do
        arr[#arr+1]=parse(); ws()
        if s:sub(i,i)==']' then i=i+1; return arr end
        if s:sub(i,i)~=',' then error('Expected comma') end
        i=i+1
      end
    end
    local tail=s:sub(i)
    if tail:sub(1,4)=='true' then i=i+4; return true end
    if tail:sub(1,5)=='false' then i=i+5; return false end
    if tail:sub(1,4)=='null' then i=i+4; return nil end
    local num=tail:match('^%-?%d+%.?%d*[eE]?[%+%-]?%d*')
    if num and #num>0 and tonumber(num) then i=i+#num; return tonumber(num) end
    error('Invalid JSON at '..i)
  end
  local obj=parse(); ws(); if i<=len then error('Trailing JSON') end
  return obj
end

-- No vkeys module required: VK codes are embedded below.
-- Windows character conversion, built into the OS (no encoding.dll required).
local win = require 'ffi'
win.cdef[[int __stdcall MultiByteToWideChar(unsigned int, unsigned long, const char*, int, void*, int);
int __stdcall WideCharToMultiByte(unsigned int, unsigned long, const void*, int, char*, int, const char*, void*);]]
local kernel = win.load('kernel32')
local function convertCodepage(text, fromcp, tocp)
  text=tostring(text or '')
  local n=kernel.MultiByteToWideChar(fromcp,0,text,#text,nil,0)
  if n<=0 then return text end
  local wide=win.new('wchar_t[?]',n)
  if kernel.MultiByteToWideChar(fromcp,0,text,#text,wide,n)<=0 then return text end
  local m=kernel.WideCharToMultiByte(tocp,0,wide,n,nil,0,nil,nil)
  if m<=0 then return text end
  local out=win.new('char[?]',m)
  if kernel.WideCharToMultiByte(tocp,0,wide,n,out,m,nil,nil)<=0 then return text end
  return win.string(out,m)
end
local u8 = setmetatable({}, {__call=function(_,text) return convertCodepage(text,1251,65001) end})
function u8:decode(text) return convertCodepage(text,65001,1251) end
function u8:encode(text) return convertCodepage(text,1251,65001) end

-- MoonLoader/SA:MP usually supplies chat-command arguments in CP1251.
-- Some clients/mods may pass UTF-8 already; converting that twice corrupts Russian.
local function isValidUtf8(s)
  if type(s) ~= 'string' then return false end
  local i, n = 1, #s
  while i <= n do
    local b = s:byte(i)
    if b < 0x80 then i = i + 1
    else
      local len
      if b >= 0xC2 and b <= 0xDF then len = 2
      elseif b >= 0xE0 and b <= 0xEF then len = 3
      elseif b >= 0xF0 and b <= 0xF4 then len = 4
      else return false end
      if i + len - 1 > n then return false end
      local b2 = s:byte(i+1)
      if b2 < 0x80 or b2 > 0xBF then return false end
      if len >= 3 then
        local b3 = s:byte(i+2)
        if b3 < 0x80 or b3 > 0xBF then return false end
        if b == 0xE0 and b2 < 0xA0 then return false end
        if b == 0xED and b2 > 0x9F then return false end
      end
      if len == 4 then
        local b4 = s:byte(i+3)
        if b4 < 0x80 or b4 > 0xBF then return false end
        if b == 0xF0 and b2 < 0x90 then return false end
        if b == 0xF4 and b2 > 0x8F then return false end
      end
      i = i + len
    end
  end
  return true
end

local function gameInputToUtf8(s)
  if isValidUtf8(s) then return s end
  return u8:encode(s)
end
local new = hasImgui and imgui.new or nil
-- IMPORTANT: Change this address to the exact Render service where main.py is deployed.
local BASE = 'https://samp-messenger-server-18.onrender.com'
local DIR = getWorkingDirectory() .. '\\SAMP_Messenger\\'
local BG_DIR = DIR .. 'backgrounds\\'
local BIND_FILE = DIR .. 'binds.json'
local window = hasImgui and new.bool(false) or {[0]=false}
local helpWindow = hasImgui and new.bool(false) or {[0]=false}
local helpSection = 1 -- 1: messenger, 2: GPS
local helpCyrillicFont = nil
local active_tab = 1 -- 1: all chat; 2: my messages; 3: bind editor
local history_page = 0 -- 80 lines per page to avoid lowering FPS
local input, bind_text = hasImgui and new.char[1025]() or ffi.new('char[1025]'), hasImgui and new.char[256]() or ffi.new('char[256]')
local status, messages, online, backgrounds = 'Waiting for SA:MP...', {}, {}, {}
local HISTORY_FILE = nil
local HISTORY_RAM_LIMIT = 1500 -- the full history remains on disk
local selected, current_texture = 0, nil
local shared = {nick='', running=true, output='', error='', connected=false, sent_total=0}
local worker, binds, capture, last_hotkey = nil, {}, false, 0
local selected_bind = 0
local bind_name = hasImgui and new.char[100]() or ffi.new('char[100]')
local last_editor_save = ''
local key_preview = ''
local capture_key, capture_ctrl, capture_alt, capture_shift = 0,false,false,false
local previous_keys = {}
local save_clock = 0
local editor_dirty = false
local MAX_BIND_CHARS = 60
local show_in_game = true
local edit_key, edit_ctrl, edit_alt, edit_shift = 0, false, false, false
local bind_target = 1  -- 1: Messenger; 2: SA:MP chat
local keynames = {}
for c=65,90 do keynames[c] = string.char(c) end
for c=48,57 do keynames[c] = string.char(c) end
for i=1,12 do keynames[111+i] = 'F' .. i end
for _, item in ipairs({{32,'SPACE'},{9,'TAB'},{13,'ENTER'},{8,'BACKSPACE'},
  {45,'INSERT'},{36,'HOME'},{35,'END'},{33,'PGUP'},{34,'PGDN'},
  {37,'LEFT'},{38,'UP'},{39,'RIGHT'},{40,'DOWN'},
  {96,'NUM0'},{97,'NUM1'},{98,'NUM2'},{99,'NUM3'},{100,'NUM4'},
  {101,'NUM5'},{102,'NUM6'},{103,'NUM7'},{104,'NUM8'},{105,'NUM9'}}) do
  keynames[item[1]] = item[2]
end
local watch_keys = {}
for n in pairs(keynames) do watch_keys[#watch_keys+1] = n end
table.sort(watch_keys)

local function makeDir()
  if not doesDirectoryExist(DIR) then createDirectory(DIR) end
  if not doesDirectoryExist(BG_DIR) then createDirectory(BG_DIR) end
end
local function setBuffer(buf, value)
  ffi.fill(buf, ffi.sizeof(buf))
  ffi.copy(buf, tostring(value or ''):sub(1, ffi.sizeof(buf)-1))
end
local function scanBackgrounds()
  backgrounds = {}
  local handle, file = findFirstFile(BG_DIR .. '*')
  if handle then
    while file do
      if file:lower():match('%.png$') or file:lower():match('%.jpe?g$') then
        backgrounds[#backgrounds+1] = file
      end
      file = findNextFile(handle)
    end
    findClose(handle)
  end
  table.sort(backgrounds)
end
local function loadBackground(index)
  selected, current_texture = index, nil
  if index > 0 and backgrounds[index] then
    local ok, texture = pcall(imgui.CreateTextureFromFile, BG_DIR .. backgrounds[index])
    if ok and texture then current_texture = texture
    else status = 'Image load error: ' .. tostring(texture) end
  end
end

-- Networking runs in a separate Windows PowerShell process (included with Windows).
-- This Lua file does not require Effil, LuaSocket or LuaSec.
local SESSION = DIR .. 'session_' .. tostring(os.time()) .. '_' .. tostring(math.random(1000,9999)) .. '\\'
local OUTBOX = SESSION .. 'outgoing\\'
local STATE = SESSION .. 'state.json'
local STATE_ACK = SESSION .. 'state_ack.txt'
local HEARTBEAT = SESSION .. 'alive.txt'
local BRIDGE = SESSION .. 'bridge.ps1'
local WORKER_ALIVE = SESSION .. 'worker_alive.txt'
local GPS_INPUT = SESSION .. 'gps_input.json'
local GPS_STATE = SESSION .. 'gps_state.json'
local GPS_STATUS = SESSION .. 'gps_status.json'
local GPS_TOKEN = SESSION .. 'gps_token.txt'
local GPS_BRIDGE = SESSION .. 'gps_bridge.ps1'
local GPS_WORKER_ALIVE = SESSION .. 'gps_worker_alive.txt'
local NETLOG = SESSION .. 'network.log'
local LAUNCHLOG = SESSION .. 'launch.log'
local bridgeReady = false
local lastHeart = 0
local lastStateCheck = 0
local lastStateSeq = -1
local counter = 0
local lastWorkerWatch = 0
local lastNetworkLaunch = 0
local workerStartTime = 0
local shell = ffi.load('shell32')
ffi.cdef[[
  void* __stdcall ShellExecuteA(void* hwnd, const char* operation, const char* file,
      const char* parameters, const char* directory, int showCmd);
]]
local function writeText(file, value)
  local f = io.open(file, 'wb')
  if not f then return false end
  f:write(value)
  f:close()
  return true
end

-- Silent GitHub updater (runs separately and never modifies the active in-memory Lua).
-- Publishes a backup with .bak extension in SAMP_Messenger/backups/.
local AUTO_UPDATE_VERSION = '1.1.9'
local AUTO_UPDATE_INTERVAL_SECONDS = 1800
local AUTO_UPDATE_HELPER = SESSION .. 'auto_updater.ps1'
local lastAutoUpdateCheck = -999999
local UPDATE_PS = [==[
param(
  [Parameter(Mandatory=$true)][string]$source,
  [Parameter(Mandatory=$true)][string]$installedVersion,
  [Parameter(Mandatory=$true)][string]$dataDir
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Only these fixed HTTPS addresses are used, never a URL from version.json.
$manifestUrl = 'https://raw.githubusercontent.com/flamesmall0-wq/samp-messenger-server/main/updates/version.json'
$downloadUrl = 'https://raw.githubusercontent.com/flamesmall0-wq/samp-messenger-server/main/updates/SAMP_Messenger.lua'
$logFile = Join-Path $dataDir 'updater.log'
$markerFile = Join-Path $dataDir 'update_installed.txt'
$mutex = [Threading.Mutex]::new($false, 'Local\SAMP_Messenger_AutoUpdate')
$ownsMutex = $false
function Log([string]$msg) {
  try { [IO.File]::AppendAllText($logFile, (Get-Date -Format s) + ' ' + $msg + [Environment]::NewLine) } catch {}
}
function VersionParts([string]$v) {
  $m = [regex]::Match($v, '^(\d{1,4})\.(\d{1,4})\.(\d{1,4})$')
  if (-not $m.Success) { throw ('Invalid version: ' + $v) }
  return @([int]$m.Groups[1].Value, [int]$m.Groups[2].Value, [int]$m.Groups[3].Value)
}
function Newer([string]$candidate, [string]$current) {
  $a = VersionParts $candidate
  $b = VersionParts $current
  for ($i = 0; $i -lt 3; $i++) {
    if ($a[$i] -gt $b[$i]) { return $true }
    if ($a[$i] -lt $b[$i]) { return $false }
  }
  return $false
}
try {
  try { $ownsMutex = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
  if (-not $ownsMutex) { exit 0 }
  if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw 'Installed Lua file is missing' }
  $manifest = Invoke-RestMethod -Uri $manifestUrl -TimeoutSec 15 -Method Get
  $nextVersion = [string]$manifest.version
  $expectedHash = ([string]$manifest.sha256).ToLowerInvariant()
  if ($expectedHash -notmatch '^[0-9a-f]{64}$') { throw 'Invalid update SHA-256' }
  if (-not (Newer $nextVersion $installedVersion)) { exit 0 }
  if (Test-Path -LiteralPath $markerFile) {
    $already = ([IO.File]::ReadAllText($markerFile)).Trim()
    if ($already -eq $nextVersion) { exit 0 }
  }
  $destinationDir = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($source))
  $stagePath = Join-Path $destinationDir ([IO.Path]::GetFileName($source) + '.download.' + [guid]::NewGuid().ToString('N') + '.tmp')
  try {
    Invoke-WebRequest -Uri $downloadUrl -OutFile $stagePath -UseBasicParsing -TimeoutSec 30 | Out-Null
    $length = (Get-Item -LiteralPath $stagePath).Length
    if ($length -lt 10000 -or $length -gt 2097152) { throw ('Unexpected update size: ' + $length) }
    $actualHash = (Get-FileHash -LiteralPath $stagePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) { throw 'Downloaded file SHA-256 does not match manifest' }
    # Reject an obviously wrong artifact even when the manifest matches it.
    $first = ([IO.File]::ReadAllText($stagePath, [Text.Encoding]::UTF8))
    if (-not $first.Contains("script_name('SAMP Messenger')") -or -not $first.Contains("script_version('" + $nextVersion + "')")) {
      throw 'Downloaded file is not the expected SAMP Messenger Lua version'
    }
    $backups = Join-Path $dataDir 'backups'
    if (-not (Test-Path -LiteralPath $backups)) { New-Item -ItemType Directory -Path $backups -Force | Out-Null }
    $timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    # .bak is intentional: MoonLoader must never execute the old version.
    $backupPath = Join-Path $backups ('SAMP_Messenger_v' + $installedVersion + '_' + $timestamp + '.bak')
    $suffix = 1
    while (Test-Path -LiteralPath $backupPath) {
      $backupPath = Join-Path $backups ('SAMP_Messenger_v' + $installedVersion + '_' + $timestamp + '_' + $suffix + '.bak')
      $suffix++
    }
    # Atomic on NTFS where available. A fallback still preserves the old source.
    try {
      [IO.File]::Replace($stagePath, $source, $backupPath)
    } catch {
      [IO.File]::Copy($source, $backupPath, $false)
      [IO.File]::Copy($stagePath, $source, $true)
    }
    [IO.File]::WriteAllText($markerFile, $nextVersion, [Text.Encoding]::ASCII)
    Log ('Installed v' + $nextVersion + ' (was v' + $installedVersion + '); backup=' + $backupPath + '; restart SA-MP/MoonLoader to activate')
  } finally {
    if (Test-Path -LiteralPath $stagePath) { Remove-Item -LiteralPath $stagePath -Force -ErrorAction SilentlyContinue }
  }
} catch {
  Log ('Update check failed: ' + $_.Exception.Message)
} finally {
  if ($ownsMutex) { try { $mutex.ReleaseMutex() } catch {} }
  $mutex.Dispose()
}

]==]
local function autoUpdateScriptPath()
  local ok, path = pcall(function() return thisScript().path end)
  if ok and type(path)=='string' and path~='' then return path end
  local info=debug.getinfo(autoUpdateScriptPath,'S')
  local source=info and info.source or ''
  if source:sub(1,1)=='@' then return source:sub(2) end
  return nil
end
local function checkAutoUpdate()
  local now = getGameTimer()/1000
  if now-lastAutoUpdateCheck < AUTO_UPDATE_INTERVAL_SECONDS then return end
  lastAutoUpdateCheck = now
  local source = autoUpdateScriptPath()
  if not source or source:find('"',1,true) or source:find('[\r\n]') then return end
  if not doesDirectoryExist(SESSION) then createDirectory(SESSION) end
  if not writeText(AUTO_UPDATE_HELPER, UPDATE_PS) then return end
  local store = DIR:gsub('[/\\]+$', '')
  local args = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "'
    ..AUTO_UPDATE_HELPER..'" "'..source..'" "'..AUTO_UPDATE_VERSION..'" "'..store..'"'
  pcall(function() shell.ShellExecuteA(nil,'open','powershell.exe',args,nil,0) end)
end
-- Single two-second status toast. There is deliberately NO offline message queue.
local toast = {text='', until_ms=0}
local toastFont = nil
local function showToast(text)
  toast.text = tostring(text or '')
  toast.until_ms = getGameTimer() + 2000
end
local function utf8Short(s, maxChars)
  s = tostring(s or ''):gsub('[\r\n]+',' ')
  local pos, count = 1, 0
  while pos <= #s and count < maxChars do
    local b=s:byte(pos)
    local size=b<128 and 1 or (b<224 and 2 or (b<240 and 3 or 4))
    pos=pos+size
    count=count+1
  end
  if pos<=#s then return s:sub(1,pos-1)..'...' end
  return s
end
local function drawOverlayLine(value,x,y,color)
  local text=value
  local ok,decoded=pcall(function() return u8:decode(value) end)
  if ok then text=decoded end
  pcall(renderFontDrawText,toastFont,text,x+1,y+1,0xE0000000)
  pcall(renderFontDrawText,toastFont,text,x,y,color)
end
local function renderToast()
  if toast.text=='' or getGameTimer()>=toast.until_ms then return end
  if not toastFont and type(renderCreateFont)=='function' then
    local ok,font=pcall(renderCreateFont,'Arial',10,5)
    if ok then toastFont=font end
  end
  if not toastFont or type(renderFontDrawText)~='function' then return end
  local height=768
  if type(getScreenResolution)=='function' then
    local ok,w,h=pcall(getScreenResolution)
    if ok and type(h)=='number' and h>0 then height=h end
  end
  drawOverlayLine(utf8Short(toast.text,95),24,math.floor(height*0.31),0xFF9EF0A0)
end

-- Permanent local history (JSON Lines); loading keeps only recent lines in RAM.
local function rememberMessage(entry, save)
  if type(entry)~='table' or type(entry.text)~='string' then return end
  local record={
    nick=tostring(entry.nick or '?'), text=entry.text,
    date=tostring(entry.date or os.date('%d.%m.%Y %H:%M:%S')),
    source=tostring(entry.source or 'game'), me=entry.me==true
  }
  messages[#messages+1]=record
  while #messages>HISTORY_RAM_LIMIT do table.remove(messages,1) end
  if save and HISTORY_FILE then
    local ok, line=pcall(json.encode, record)
    if ok then
      local f=io.open(HISTORY_FILE,'ab')
      if f then f:write(line, '\n'); f:close() end
    end
  end
end
local function loadHistory(nick)
  local safeNick=tostring(nick or 'player'):gsub('[^%w_%-]','_')
  HISTORY_FILE=DIR..'history_'..safeNick..'.jsonl'
  local f=io.open(HISTORY_FILE,'rb')
  if not f then return end
  for line in f:lines() do
    local ok, item=pcall(json.decode,line)
    if ok and type(item)=='table' then rememberMessage(item,false) end
  end
  f:close()
end

local PS = [==[
param([string]$base,[string]$dir,[string]$nick,[string]$queue)
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$log = Join-Path $dir 'network.log'
$workerFile = Join-Path $dir 'worker_alive.txt'
$alive = Join-Path $dir 'alive.txt'
$ackFile = Join-Path $dir 'state_ack.txt'
# The separate GPS bridge reads the same session token without delaying chat.
function Log([string]$line) {
  try { [IO.File]::AppendAllText($log, ((Get-Date).ToString('HH:mm:ss') + ' ' + $line + [Environment]::NewLine), $utf8) } catch {}
}
function TouchWorker {
  try { [IO.File]::WriteAllText($workerFile, [string][DateTimeOffset]::UtcNow.ToUnixTimeSeconds(), $utf8) } catch {}
}
# Only one background worker for a session; avoids duplicate sends after watchdog restarts.
$hash = [Security.Cryptography.SHA256]::Create()
try {
  $identifier = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($dir))).Replace('-','').Substring(0,24)
} finally { $hash.Dispose() }
$mutex = [System.Threading.Mutex]::new($false, ('Local\SAMP_Messenger_' + $identifier))
$ownsMutex = $false
try { $ownsMutex = $mutex.WaitOne(1500) } catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
if (-not $ownsMutex) { Log 'Second worker blocked (existing process running)'; $mutex.Dispose(); exit 0 }
Log ('Worker started; nick=' + $nick + '; server=' + $base)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$token = $null
$after = 0
$online = $false
$lastError = ''
$lastJoin = [datetime]::MinValue
$sentTotal = 0
$sentNotifications = @()
$waitingSeq = 0
$waitingAfter = 0
$lastLoggedError = ''
$lastLogTime = [datetime]::MinValue
function PostJson([string]$path, $body) {
  $payload = ConvertTo-Json -InputObject $body -Compress -Depth 7
  return Invoke-RestMethod -Uri ($base + $path) -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($payload)) -TimeoutSec 15
}
# Read raw response as UTF-8 to preserve Cyrillic even on Windows PowerShell 5.1.
function GetJsonUtf8([string]$uri) {
  $req = [Net.HttpWebRequest]::Create($uri)
  $req.Method = 'GET'
  $req.Timeout = 15000
  $req.ReadWriteTimeout = 15000
  $resp = $req.GetResponse()
  try {
    $reader = [IO.StreamReader]::new($resp.GetResponseStream(), [Text.Encoding]::UTF8, $true)
    try { $raw = $reader.ReadToEnd() } finally { $reader.Dispose() }
  } finally { $resp.Dispose() }
  return ConvertFrom-Json -InputObject $raw
}
# File.Replace requires a valid backup path on some PowerShell/.NET installations.
# Use an explicit backup filename and fall back to Copy on unsupported filesystems.
# If Lua catches a partial fallback copy, the worker republishes after an ACK timeout.
$stateSeq = 0
$lastStateJson = ''
function StateWrite($payload) {
  $script:stateSeq++
  $dest = Join-Path $dir 'state.json'
  $tmp = Join-Path $dir ('state_' + [guid]::NewGuid().ToString('N') + '.tmp')
  $backup = Join-Path $dir 'state_replace_backup.tmp'
  $obj = @{ seq=$stateSeq; connected=$online; error=$lastError; data=$payload; sent_total=$sentTotal; sent_messages=@($sentNotifications); failed_messages=@($failedNotifications); accepted_messages=@($acceptedNotifications) }
  $jsonText = ConvertTo-Json -InputObject $obj -Compress -Depth 15
  try {
    [IO.File]::WriteAllText($tmp, $jsonText, $utf8)
    if ([IO.File]::Exists($dest)) {
      try {
        [IO.File]::Replace($tmp, $dest, $backup)
      } catch {
        # Windows PowerShell 5.1 / filesystem compatibility fallback.
        [IO.File]::Copy($tmp, $dest, $true)
      }
    } else {
      try { [IO.File]::Move($tmp, $dest) }
      catch [IO.IOException] { [IO.File]::Copy($tmp, $dest, $true) }
    }
    $script:lastStateJson = $jsonText
    return $true
  } catch {
    Log ('State write error: ' + $_.Exception.Message)
    return $false
  } finally {
    if ([IO.File]::Exists($tmp)) { try { [IO.File]::Delete($tmp) } catch {} }
  }
}
function RefreshUnackedState {
  if ([string]::IsNullOrEmpty($lastStateJson)) { return }
  try { [IO.File]::WriteAllText((Join-Path $dir 'state.json'), $lastStateJson, $utf8) } catch {}
}
$failedNotifications = @()
$retryById = @{}
$lastAwaitingAck = [datetime]::MinValue
$acceptedNotifications = @()

try {
  while ($true) {
    # Retain delivery notifications until a state snapshot is written.
    TouchWorker
    if (-not (Test-Path -LiteralPath $alive)) { break }
    if (((Get-Date) - (Get-Item -LiteralPath $alive).LastWriteTime).TotalSeconds -gt 20) { break }
    try {
      if (-not $token) {
        if (((Get-Date) - $lastJoin).TotalSeconds -lt 3) {
          StateWrite $null
          Start-Sleep -Milliseconds 500
          continue
        }
        $lastJoin = Get-Date
        # One code per Windows installation, shared by all SA:MP nicknames.
        # Never pass it in command-line parameters, logs or public Lua files.
        $keyFile = Join-Path (Split-Path -Parent $dir) 'access_code.txt'
        if (-not [IO.File]::Exists($keyFile)) { throw 'No access code. Enter /gkey CODE in SA:MP.' }
        $accessKey = [IO.File]::ReadAllText($keyFile, [Text.Encoding]::UTF8).Trim()
        if ($accessKey -notmatch '^[A-Za-z0-9_-]{8,128}$') { throw 'Access code invalid. Re-enter /gkey.' }
        $join = PostJson '/join' @{nick=$nick; access_key=$accessKey}
        if (-not $join.token) { throw 'Join returned no session token' }
        $token = [string]$join.token
        try { [IO.File]::WriteAllText((Join-Path $dir 'gps_token.txt'), $token, $utf8) } catch { Log ('GPS token file error: ' + $_.Exception.Message) }
        $after = [long]$join.last_id
        $online = $true
        $lastError = ''
        $waitingSeq = 0
        Log 'Join successful'
      }
      $sendError = ''
      # Outgoing files are per-session, in-flight ONLY. Never replay old backlog.
      $files = @(Get-ChildItem -LiteralPath $queue -Filter '*.json' -File -ErrorAction SilentlyContinue | Sort-Object Name)
      foreach ($entry in $files) {
        if (-not $token) { break }
        $messageText = ''
        try {
          $raw = [IO.File]::ReadAllText($entry.FullName, [Text.Encoding]::UTF8)
          $body = ConvertFrom-Json -InputObject $raw
          $messageText = [string]$body.text
          $msgId = [IO.Path]::GetFileNameWithoutExtension($entry.Name)
          if ($body.message_id) { $msgId = [string]$body.message_id }
          $answer = PostJson '/send' @{token=$token; text=$messageText; message_id=$msgId}
          if ($answer.ok -ne $true) { throw 'Send API returned ok=false' }
          $receipt = $null
          $legacyReceipt = $false
          try {
            $receipt = GetJsonUtf8 ($base + '/delivery?token=' + [uri]::EscapeDataString($token) + '&message_id=' + [uri]::EscapeDataString($msgId))
          } catch {
            if ($_.Exception.Message -match '404|Not Found') {
              $legacyReceipt = $true
              Log 'Server lacks /delivery. Message accepted by Render, Telegram unverified.'
            } else { throw }
          }
          $deliveryStatus = if ($legacyReceipt) { 'server_accepted' } else { [string]$receipt.status }
          if ($deliveryStatus -eq 'server_accepted') {
            Remove-Item -LiteralPath $entry.FullName -Force -ErrorAction SilentlyContinue
            $retryById.Remove($msgId)
            $acceptedNotifications += $messageText
          } elseif ($deliveryStatus -eq 'delivered') {
            Remove-Item -LiteralPath $entry.FullName -Force -ErrorAction SilentlyContinue
            $retryById.Remove($msgId)
            $sentTotal++
            $sentNotifications += $messageText
          } elseif ($deliveryStatus -eq 'pending') {
            $retryById[$msgId] = [int]$retryById[$msgId] + 1
            if ([int]$retryById[$msgId] -ge 15) { throw 'Telegram delivery timed out' }
            # Keep only the single in-flight file while Telegram is processing it.
            Start-Sleep -Milliseconds 500
            break
          } else {
            throw ('Telegram delivery: ' + $deliveryStatus + ' ' + [string]$receipt.error)
          }
        } catch {
          $sendError = 'Send/Telegram: ' + $_.Exception.Message
          # Do not keep a waiting queue or replay after reboot.
          $failedNotifications += $messageText
          try { Remove-Item -LiteralPath $entry.FullName -Force -ErrorAction Stop } catch {}
          if ($sendError -match '\(401\)|\(403\)|401|403') { $token=$null; $online=$false }
          Log $sendError
          break
        }
      }
      if ($token) {
        # Wait for MoonLoader to acknowledge each state snapshot. Otherwise a
        # slower PC can miss a snapshot when PowerShell replaces state.json.
        if ($waitingSeq -gt 0) {
          $ackNum = -1
          try {
            if ([IO.File]::Exists($ackFile)) {
              $ackNum = [long]([IO.File]::ReadAllText($ackFile, $utf8).Trim())
            }
          } catch {}
          if ($ackNum -eq $waitingSeq) {
            $after = [math]::Max($after, $waitingAfter)
            $waitingSeq = 0
          } elseif (((Get-Date)-$lastAwaitingAck).TotalSeconds -ge 2) {
            RefreshUnackedState
            $lastAwaitingAck = Get-Date
          }
        }
        if ($waitingSeq -eq 0) {
          $state = GetJsonUtf8 ($base + '/state?token=' + [uri]::EscapeDataString($token) + '&after=' + $after)
          $online = $true
          $lastError = $sendError
          if (StateWrite $state) {
            $waitingSeq = $stateSeq
            $waitingAfter = [long]$state.last_id
            $lastAwaitingAck = Get-Date
            $sentNotifications = @()
            $failedNotifications = @()
            $acceptedNotifications = @()
          } else {
            $lastError = 'State file write failed; retrying'
          }
        }
      } else {
        $lastError = $sendError
        StateWrite $null
      }
      $lastLoggedError = ''
    } catch {
      $lastError = $_.Exception.Message
      $online = $false
      if ($lastError -match '401|403') { $token = $null }
      # 409: previous connection with same nick still registered; retry after its timeout.
      if ($lastError -ne $lastLoggedError -or ((Get-Date)-$lastLogTime).TotalSeconds -ge 20) {
        Log ('Network error: ' + $lastError)
        $lastLoggedError = $lastError
        $lastLogTime = Get-Date
      }
      StateWrite $null
      Start-Sleep -Milliseconds 900
    }
    TouchWorker
    Start-Sleep -Milliseconds 300
  }
  Log 'Worker shutdown'
  if ($token) { try { $null = PostJson '/leave' @{token=$token} } catch {} }
} finally {
  try { $mutex.ReleaseMutex() } catch {}
  $mutex.Dispose()
}
]==]
-- Independent GPS background process; only exchanges files with Lua, never touches /g.
local GPS_PS = [==[
param([string]$base,[string]$dir)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$utf8 = [Text.UTF8Encoding]::new($false)
$alive = Join-Path $dir 'alive.txt'
$tokenFile = Join-Path $dir 'gps_token.txt'
$input = Join-Path $dir 'gps_input.json'
$state = Join-Path $dir 'gps_state.json'
$stat = Join-Path $dir 'gps_status.json'
$workerFile = Join-Path $dir 'gps_worker_alive.txt'
$log = Join-Path $dir 'gps_network.log'
$hash = [Security.Cryptography.SHA256]::Create()
try { $id=[BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($dir))).Replace('-','').Substring(0,24) } finally { $hash.Dispose() }
$mutex = [Threading.Mutex]::new($false, ('Local\SAMP_GPS_' + $id))
$mine = $false
try { $mine = $mutex.WaitOne(100) } catch [Threading.AbandonedMutexException] { $mine = $true }
if (-not $mine) { $mutex.Dispose(); exit 0 }
function SaveJson([string]$path, $value) {
  $tmp = $path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
  $backup = $path + '.bak'
  try {
    $raw = ConvertTo-Json -InputObject $value -Compress -Depth 8
    [IO.File]::WriteAllText($tmp, $raw, $utf8)
    if ([IO.File]::Exists($path)) {
      try { [IO.File]::Replace($tmp, $path, $backup) }
      catch { [IO.File]::Copy($tmp, $path, $true) }
    } else { [IO.File]::Move($tmp, $path) }
  } finally { if ([IO.File]::Exists($tmp)) { try { [IO.File]::Delete($tmp) } catch {} } }
}
function GpsGet([string]$url) {
  return Invoke-RestMethod -Uri $url -Method Get -TimeoutSec 4
}
function GpsPost([string]$url, $body) {
  $payload = ConvertTo-Json -InputObject $body -Compress -Depth 6
  return Invoke-RestMethod -Uri $url -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($payload)) -TimeoutSec 4
}
$lastError = ''
try {
  while ([IO.File]::Exists($alive)) {
    $started = Get-Date
    try {
      [IO.File]::WriteAllText($workerFile, [string][DateTimeOffset]::UtcNow.ToUnixTimeSeconds(), $utf8)
      if (((Get-Date) - (Get-Item -LiteralPath $alive).LastWriteTime).TotalSeconds -gt 20) { break }
      if (-not ([IO.File]::Exists($tokenFile) -and [IO.File]::Exists($input))) { throw 'Waiting for connection / GPS input' }
      $token = [IO.File]::ReadAllText($tokenFile, $utf8).Trim()
      if (-not $token) { throw 'Waiting for SAMP connection' }
      $data = ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($input, $utf8))
      $body = @{token=$token;share=([bool]$data.share);cuffed=([bool]$data.cuffed)}
      if ($body.share) {
        $body.x = [double]$data.x
        $body.y = [double]$data.y
        $body.z = [double]$data.z
      }
      $r = GpsPost ($base + '/gps/update') $body
      if ($r.ok -ne $true) { throw '/gps/update returned ok=false' }
      $players = @()
      if ([bool]$data.show) {
        $res = GpsGet ($base + '/gps/list?token=' + [uri]::EscapeDataString($token))
        if ($res.ok -ne $true) { throw '/gps/list returned ok=false' }
        if ($null -ne $res.players) { $players = @($res.players) }
      }
      $ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
      SaveJson $state @{ at=$ts;players=$players }
      SaveJson $stat @{ok=$true;at=$ts;count=$players.Count;error=''}
      $lastError = ''
    } catch {
      $e = [string]$_.Exception.Message
      if ($e -ne $lastError) {
        try { [IO.File]::AppendAllText($log, ((Get-Date).ToString('HH:mm:ss') + ' GPS: ' + $e + [Environment]::NewLine), $utf8) } catch {}
        $lastError = $e
      }
      try { SaveJson $stat @{ok=$false;at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds();count=0;error=$e} } catch {}
    }
    $ms = [int]((Get-Date) - $started).TotalMilliseconds
    # Best effort once per second; network requests may take longer.
    Start-Sleep -Milliseconds ([math]::Max(50,1000-$ms))
  }
} finally {
  try { $mutex.ReleaseMutex() } catch {}
  $mutex.Dispose()
}

]==]
local gpsLastLaunch=0
local function gpsLaunch()
  if not bridgeReady then return false end
  if not writeText(GPS_BRIDGE, GPS_PS) then return false end
  local now=os.time()
  if now-gpsLastLaunch<7 then return false end
  gpsLastLaunch=now
  local sessionArg=SESSION:gsub('[/\\]+$','')
  local params='-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "'..GPS_BRIDGE..'" "'..BASE..'" "'..sessionArg..'"'
  local rc=shell.ShellExecuteA(nil,'open','powershell.exe',params,nil,0)
  local code=tonumber(ffi.cast('intptr_t',rc))
  return code and code>32
end
local function launchNetwork(nick)
  if not doesDirectoryExist(SESSION) then createDirectory(SESSION) end
  if not doesDirectoryExist(OUTBOX) then createDirectory(OUTBOX) end
  if not doesDirectoryExist(OUTBOX) then return false, 'Cannot create outgoing directory' end
  if not writeText(BRIDGE, PS) then return false, 'Cannot write network helper' end
  if not writeText(HEARTBEAT, tostring(os.time())) then return false, 'Cannot write heartbeat' end
  -- IMPORTANT: a quoted Windows command-line argument must NOT end in a backslash.
  -- SESSION always ends with '\', which would escape the final quote in many Windows parsers.
  -- Pass the same directory without its trailing separator; PS uses Join-Path internally.
  local sessionArg = SESSION:gsub('[/\\]+$', '')
  local outboxArg = OUTBOX:gsub('[/\\]+$', '')
  local params = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' .. BRIDGE .. '" "' .. BASE .. '" "' .. sessionArg .. '" "' .. nick .. '" "' .. outboxArg .. '"'
  writeText(LAUNCHLOG, 'PowerShell launch requested\r\nScript: ' .. BRIDGE .. '\r\nSession: ' .. sessionArg .. '\r\n')
  local rc = shell.ShellExecuteA(nil, 'open', 'powershell.exe', params, nil, 0)
  local code = tonumber(ffi.cast('intptr_t', rc))
  local f = io.open(LAUNCHLOG, 'ab')
  if f then f:write('ShellExecuteA result: ' .. tostring(code) .. '\r\n'); f:close() end
  if code <= 32 then
    return false, 'Cannot launch Windows PowerShell (ShellExecuteA=' .. tostring(code) .. ')'
  end
  bridgeReady = true
  gpsLaunch()
  workerStartTime = os.time()
  lastNetworkLaunch = os.time()
  shared.connected = false
  shared.error = 'Connecting to Render...'
  return true
end
local function networkTick()
  if not bridgeReady then return end
  local now = getGameTimer()/1000
  if now-lastHeart > 1 then
    writeText(HEARTBEAT, tostring(os.time()))
    lastHeart = now
  end
  if now-lastStateCheck >= 0.15 then
    lastStateCheck = now
    local f=io.open(STATE,'rb')
    if f then
      local raw=f:read('*a'); f:close()
      -- Do not delete state.json: PowerShell may be replacing it at this moment.
      local ok, packet=pcall(json.decode, raw)
      if ok and type(packet)=='table' and type(packet.seq)=='number' and packet.seq ~= lastStateSeq then
        lastStateSeq = packet.seq
        shared.connected = packet.connected == true
        shared.error = tostring(packet.error or '')
        local confirmed = tonumber(packet.sent_total) or 0
        if confirmed > shared.sent_total then
          local sent = packet.sent_messages
          if type(sent)=='table' and #sent>0 then
            -- Show the last actually confirmed text rather than `send 1`.
            showToast('Отправлено в Telegram: ' .. tostring(sent[#sent]))
          else
            showToast('Отправлено в Telegram')
          end
          shared.sent_total = confirmed
        end
        if type(packet.accepted_messages)=='table' and #packet.accepted_messages>0 then
          showToast('Передано Render (Telegram не проверен): ' .. tostring(packet.accepted_messages[#packet.accepted_messages]))
        end
        if type(packet.failed_messages)=='table' and #packet.failed_messages>0 then
          showToast('Ошибка отправки: ' .. tostring(packet.failed_messages[#packet.failed_messages]))
        end
        if type(packet.data)=='table' then shared.output=json.encode(packet.data) end
        -- The bridge waits for this acknowledgement before advancing /state.
        writeText(STATE_ACK, tostring(packet.seq))
      end
    end
  end
  -- Restart a crashed background worker; do not start a second worker during slow HTTP.
  if now-lastWorkerWatch > 5 then
    lastWorkerWatch = now
    local f = io.open(WORKER_ALIVE,'rb')
    local last = 0
    if f then last=tonumber(f:read('*a')) or 0; f:close() end
    local age = os.time() - last
    if os.time() - workerStartTime > 55 and age > 55 and os.time() - lastNetworkLaunch > 55 then
      shared.connected = false
      shared.error = 'Background worker inactive, restarting...'
      local ok, reason=launchNetwork(shared.nick)
      if not ok then shared.error=tostring(reason) end
    end
  end
end

local function limitUtf8(s, limit)
  s=tostring(s or ''):gsub('[\r\n]+',' ')
  local i,n=1,0
  while i<=#s and n<limit do
    local c=s:byte(i)
    i=i+(c<128 and 1 or (c<224 and 2 or (c<240 and 3 or 4)))
    n=n+1
  end
  return s:sub(1,i-1)
end

local function saveBinds()
  makeDir()
  local file, err = io.open(BIND_FILE, 'wb')
  if not file then status='SAVE FAILED: '..tostring(err); return false end
  local data = {}
  for _, b in ipairs(binds) do
    data[#data+1] = {name=b.name or '',key=b.key,ctrl=b.ctrl,alt=b.alt,
      shift=b.shift,target=b.target,text=limitUtf8(b.text, MAX_BIND_CHARS)}
  end
  local ok, encoded = pcall(json.encode, data)
  if not ok then file:close(); status='JSON encode error'; return false end
  local written, write_err = file:write(encoded)
  file:close()
  if not written then status='SAVE FAILED: '..tostring(write_err); return false end
  return true
end
local function loadBinds()
  local file = io.open(BIND_FILE, 'rb')
  if not file then return end
  local content = file:read('*a'); file:close()
  local ok, data = pcall(json.decode, content)
  if ok and type(data)=='table' then
    for _, b in ipairs(data) do
      if type(b)=='table' and type(b.key)=='number' and type(b.text)=='string' then
        binds[#binds+1] = {name=tostring(b.name or 'Bind'),key=b.key,
          ctrl=b.ctrl==true,alt=b.alt==true,shift=b.shift==true,
          target=b.target==2 and 2 or 1,text=limitUtf8(b.text, MAX_BIND_CHARS)}
      end
    end
  end
end
local function selectBind(index)
  editor_dirty = false
  selected_bind = index
  local b = binds[index]
  if not b then return end
  setBuffer(bind_name,b.name)
  setBuffer(bind_text,limitUtf8(b.text, MAX_BIND_CHARS))
  edit_key,edit_ctrl,edit_alt,edit_shift=b.key,b.ctrl,b.alt,b.shift
  bind_target=b.target
end
local function persistEditor()
  if selected_bind == 0 then return end
  local b = binds[selected_bind]
  if not b then return end
  b.name=ffi.string(bind_name)
  b.text=limitUtf8(ffi.string(bind_text), MAX_BIND_CHARS)
  b.key,b.ctrl,b.alt,b.shift=edit_key,edit_ctrl,edit_alt,edit_shift
  b.target=bind_target
  editor_dirty = false
  saveBinds()
end
local function markEditorDirty()
  editor_dirty = true
  save_clock = (getGameTimer()/1000)
end
local function countUtf8Chars(value)
  local _, n = tostring(value):gsub("[^\128-\191]", "")
  return n
end
local function cropUtf8(value, maxChars)
  local chars, i = 0, 1
  while i <= #value and chars < maxChars do
    local b = value:byte(i)
    local len = b < 128 and 1 or (b < 224 and 2 or (b < 240 and 3 or 4))
    i = i + len
    chars = chars + 1
  end
  return value:sub(1, i - 1)
end
local function keyLabel(b)
  if not b.key or b.key==0 then return 'Click Capture and press a key' end
  local parts = {}
  if b.ctrl then parts[#parts+1]='CTRL' end
  if b.alt then parts[#parts+1]='ALT' end
  if b.shift then parts[#parts+1]='SHIFT' end
  parts[#parts+1] = keynames[b.key] or tostring(b.key)
  return table.concat(parts, ' + ')
end
local function anyCtrl() return isKeyDown(0x11) or isKeyDown(0xA2) or isKeyDown(0xA3) end
local function anyAlt() return isKeyDown(0x12) or isKeyDown(0xA4) or isKeyDown(0xA5) end
local function anyShift() return isKeyDown(0x10) or isKeyDown(0xA0) or isKeyDown(0xA1) end
local function expandCodes(s)
  local x,y,z = getCharCoordinates(PLAYER_PED)
  local hp = math.floor(getCharHealth(PLAYER_PED) or 0)
  local armour = math.floor(getCharArmour(PLAYER_PED) or 0)
  -- Read the GTA SA map-zone key from coordinates and resolve it through GXT.
  -- getNameOfInfoZone returns a game label/key, not necessarily display text.
  local zone = 'Неизвестный район'
  local function readZone(fn)
    if not fn then return nil end
    local ok, raw = pcall(fn, x, y, z)
    if not ok or type(raw) ~= 'string' or raw == '' then return nil end
    if getGxtText then
      local success, full = pcall(getGxtText, raw)
      if success and type(full) == 'string' and full ~= '' then raw = full end
    end
    if raw == 'Unknown' or raw == 'unknown' then return nil end
    return raw
  end
  local rawZone = readZone(getNameOfInfoZone) or readZone(getNameOfZone)
  if rawZone then
    -- GTA SA game strings may be CP1251; ImGui and the messenger use UTF-8.
    local ok, asUtf8 = pcall(function() return u8(rawZone) end)
    zone = ok and asUtf8 or rawZone
  end
  local replacements = {
    nick=shared.nick, pos=string.format('%.1f, %.1f, %.1f',x,y,z),
    x=string.format('%.1f',x),y=string.format('%.1f',y),z=string.format('%.1f',z),
    hp=tostring(hp), armor=tostring(armour), zone=zone,
    time=os.date('%H:%M:%S'), date=os.date('%d.%m.%Y'),
    online=tostring(#online)
  }
  return (s:gsub('{([%w_]+)}',function(code)
    return replacements[code:lower()] or '{'..code..'}'
  end))
end
-- Recognize the old and new exact cuff alert; never use ordinary RP chat text.
local function isCuffHelpText(text, nick)
  nick = tostring(nick or '')
  return text == ('"' .. nick .. '" кафают помогите ему')
      or text == (nick .. ' в наручниках! Помогите ему — его метка находится на карте.')
end
-- SA-MP chat supports {RRGGBB} color spans. Nick and colon are darker;
-- [G] and message body are lighter. Telegram and cuff red are unchanged.
local function coloredGameChat(nick, body)
  return '{A9DBF4}[G] {6092BA}' .. tostring(nick or '?') .. ': {A9DBF4}' .. tostring(body or '')
end
-- Consistent display for players who have not yet upgraded their cuff alert.
local function shownCuffAlert(text, nick)
  if isCuffHelpText(text, nick) then
    return tostring(nick or '') .. ' в наручниках! Помогите ему — его метка находится на карте.'
  end
  return text
end

local function sendMessage(txt, target)
  txt = tostring(txt or ''):gsub('[\r\n]+', ' ')
  if txt == '' then return false end
  if target == 2 then
    -- ImGui stores UTF-8; SA:MP chat expects Windows-1251.
    local ok, converted = pcall(function() return u8:decode(txt) end)
    sampSendChat(ok and converted or txt)
    return true
  end
  -- No offline queue: do not silently promise a message will be delivered later.
  if not bridgeReady or not shared.connected then
    showToast('Нет подключения. Не отправлено: ' .. txt)
    sampAddChatMessage('[G] OFFLINE. Message not sent; try again when ONLINE.',0xFF7777)
    return false
  end
  if not doesDirectoryExist(OUTBOX) then createDirectory(OUTBOX) end
  if not doesDirectoryExist(OUTBOX) then
    sampAddChatMessage('[G] Cannot create outgoing directory!', 0xFF7777)
    return false
  end
  counter=counter+1
  -- Limit by UTF-8 bytes (not raw substring, which can cut Cyrillic characters).
  local limit, pos, last = 500, 1, 0
  while pos <= #txt do
    local b = txt:byte(pos)
    local width = b < 128 and 1 or (b < 224 and 2 or (b < 240 and 3 or 4))
    if pos + width - 1 > limit then break end
    last = pos + width - 1
    pos = last + 1
  end
  local clean = txt:sub(1, last)
  local name=string.format('%010d_%06d_%04d.json', os.time(), counter, math.random(1000,9999))
  local path=OUTBOX..name
  local tmp=path..'.tmp'
  if not writeText(tmp, json.encode({text=clean, message_id=name:gsub('%.json$', '')})) then
    sampAddChatMessage('[G] Could not save message. Check folder permissions.',0xFF7777)
    return false
  end
  if not os.rename(tmp,path) then
    os.remove(tmp)
    sampAddChatMessage('[G] Could not prepare message.',0xFF7777)
    return false
  end
  -- This message is saved immediately; when echoed by the server, do not
  -- print it in SA:MP chat again or create a duplicate history entry.
  rememberMessage({nick=shared.nick, text=clean, me=true, source='game'}, true)
  -- Show our own /g message in the local SA:MP chat, just like other players'.
  -- addMessage() skips the server echo for our nick, preventing duplicates.
  if show_in_game and isSampAvailable() then
    local cuffNotice = isCuffHelpText(clean, shared.nick)
    local chatLine = cuffNotice and ('[G] ' .. shownCuffAlert(clean, shared.nick))
      or coloredGameChat(shared.nick, clean)
    local converted, cp1251 = pcall(function() return u8:decode(chatLine) end)
    sampAddChatMessage(converted and cp1251 or chatLine, cuffNotice and 0xFF3535 or 0xA9DBF4)
  end
  showToast('Отправляется в Telegram: ' .. clean)
  return true
end

local function safeChatText(value)
  local ok, text = pcall(function() return u8:decode(tostring(value)) end)
  return ok and text or tostring(value)
end
local function addMessage(entry)
  local nickname = tostring(entry.nick or '?')
  local message = tostring(entry.text or '')
  local isTelegram = entry.source == 'telegram'
  -- The sending client already saved its message locally in sendMessage().
  if not isTelegram and nickname:lower()==tostring(shared.nick):lower() then return end
  rememberMessage({nick=(isTelegram and '[TG] ' or '') .. nickname,
    text=message, me=false, source=entry.source}, true)
  if show_in_game and isSampAvailable() then
    local prefix = (isTelegram and '[TG] ' or '[G] ') .. nickname .. ': '
    -- Telegram messages are violet; the standard /g chat stays light blue.
    local cuffNotice = not isTelegram and isCuffHelpText(message, nickname)
    local display = cuffNotice and ('[G] ' .. shownCuffAlert(message, nickname))
      or (isTelegram and (prefix .. message) or coloredGameChat(nickname, message))
    sampAddChatMessage(safeChatText(display), cuffNotice and 0xFF3535 or (isTelegram and 0xD5A0FF or 0xA9DBF4))
  end
end

-- GPS ADDON: optional radar / big map markers for online messenger participants.
-- Does not change /g, /mess history, or any existing bind / Telegram code.
local GPS_PREFS = DIR .. 'gps_settings.json'
local gps = {
  show=true, share=true, legend=true, blink=true, marks={}, latest={},
  written_at=0, read_at=0, received_at=0,
  max_markers=40,
}
local function gpsSavePrefs()
  writeText(GPS_PREFS,json.encode({show=gps.show,share=gps.share,legend=gps.legend,blink=gps.blink}))
end
local function gpsLoadPrefs()
  local f=io.open(GPS_PREFS,'rb')
  if not f then return end
  local text=f:read('*a'); f:close()
  local ok,v=pcall(json.decode,text)
  if ok and type(v)=='table' then
    if type(v.show)=='boolean' then gps.show=v.show end
    if type(v.share)=='boolean' then gps.share=v.share end
    if type(v.legend)=='boolean' then gps.legend=v.legend end
    if type(v.blink)=='boolean' then gps.blink=v.blink end
  end
end
-- Server-driven cuff evidence only: SA-MP special action 24 or actual handcuff
-- attachment RPC (model 19418). Ordinary chat text never changes this state.
-- Neither cue is universal on custom RP servers; /gcuffstatus helps diagnose.
local CUFFED_ACTION = 24
local CUFF_MODEL = 19418
local cuffEvidence = {attached={}, special=false, current=false, lastCheck=-999}
local function cuffResetLocal()
  cuffEvidence.attached={}
  cuffEvidence.special=false
  cuffEvidence.current=false
end
local function cuffLocalPlayerId()
  local ok, valid, id=pcall(sampGetPlayerIdByCharHandle, PLAYER_PED)
  if ok and valid then return id end
  return nil
end
-- Incoming RPC is sent by game server (not a chat line sent by players).
local cuffEventsLoaded, cuffEvents=pcall(require, 'lib.samp.events')
if cuffEventsLoaded and type(cuffEvents)=='table' then
  function cuffEvents.onSetPlayerAttachedObject(playerId, index, create, object)
    local selfId=cuffLocalPlayerId()
    if selfId == nil or tonumber(playerId)~=tonumber(selfId) then return end
    local slot=tonumber(index)
    if slot==nil then return end
    if create and type(object)=='table' and tonumber(object.modelId)==CUFF_MODEL then
      cuffEvidence.attached[slot]=true
    else
      -- A removed / replaced object no longer provides cuff evidence.
      cuffEvidence.attached[slot]=nil
    end
  end
  -- Clear stale attachments after death / respawn so a released player stops blinking.
  function cuffEvents.onSendDeathNotification()
    cuffResetLocal()
  end
  function cuffEvents.onSendSpawn()
    cuffResetLocal()
  end
end
local function cuffDetect()
  local now=getGameTimer()/1000
  if now-cuffEvidence.lastCheck < 0.25 then return cuffEvidence.current end
  cuffEvidence.lastCheck=now
  if type(sampIsLocalPlayerSpawned)=='function' then
    local ok, spawned=pcall(sampIsLocalPlayerSpawned)
    if ok and not spawned then cuffResetLocal(); return false end
  end
  local id=cuffLocalPlayerId()
  local action=nil
  if id~=nil and type(sampGetPlayerSpecialAction)=='function' then
    local ok,val=pcall(sampGetPlayerSpecialAction,id)
    if ok and type(val)=='number' then action=val end
  end
  cuffEvidence.special = action==CUFFED_ACTION
  local attached=false
  for _ in pairs(cuffEvidence.attached) do attached=true; break end
  cuffEvidence.current = cuffEvidence.special or attached
  return cuffEvidence.current
end

-- The nickname determines the same visible color on every client.
-- GTA SA permits a custom marker color in 0xRRGGBBAA form, while
-- renderFontDrawText accepts 0xAARRGGBB. We keep them separate.
local gpsColorCache={}
local function gpsNickColors(nickname)
  local key=tostring(nickname or ''):lower()
  if gpsColorCache[key] then
    return gpsColorCache[key].blip, gpsColorCache[key].hud
  end
  -- Simple cross-PC deterministic hash; no local RNG and no server changes.
  local hash=2166136
  for i=1,#key do hash=(hash*131+key:byte(i))%2147483647 end
  -- HSV: vivid yet legible colors. 360 hues x multiple saturation/value levels.
  local hue=(hash%3600)/600 -- six sectors; 0...6
  local saturation=0.73+(math.floor(hash/3600)%4)*0.055
  local value=0.88+(math.floor(hash/14400)%3)*0.055
  local c=value*saturation
  local sector=math.floor(hue)
  local x=c*(1-math.abs((hue%2)-1))
  local r,g,b=0,0,0
  if sector==0 then r,g,b=c,x,0
  elseif sector==1 then r,g,b=x,c,0
  elseif sector==2 then r,g,b=0,c,x
  elseif sector==3 then r,g,b=0,x,c
  elseif sector==4 then r,g,b=x,0,c
  else r,g,b=c,0,x end
  local m=value-c
  local rr=math.min(255,math.floor((r+m)*255+0.5))
  local gg=math.min(255,math.floor((g+m)*255+0.5))
  local bb=math.min(255,math.floor((b+m)*255+0.5))
  local rgb=rr*65536+gg*256+bb
  local rgba=rgb*256+255 -- 0xRRGGBBAA (GTA custom blip)
  if rgba>=2147483648 then rgba=rgba-4294967296 end -- signed int32
  local hud=4278190080+rgb -- 0xFFRRGGBB (text overlay)
  gpsColorCache[key]={blip=rgba,hud=hud}
  return rgba,hud
end

local function gpsRemoveMarkers()
  for _,entry in pairs(gps.marks) do
    if entry.blip then pcall(removeBlip,entry.blip) end
  end
  gps.marks={}
end
-- Native GTA radar shows a filled triangle with a dark outline for
-- a blip below the local player's height. Only visual blip Z is offset;
-- shared REAL coordinates (including true Z) are not changed.
local function gpsApplyPositions(players)
  if not gps.show then gpsRemoveMarkers(); return end
  if type(players)~='table' then return end
  local active={}
  local count=0
  local okp,px,py,pz=pcall(getCharCoordinates,PLAYER_PED)
  local zbase=okp and type(pz)=='number' and pz or 0
  for _,p in ipairs(players) do
    if type(p)=='table' then
      local nick=tostring(p.nick or '')
      local x,y,z=tonumber(p.x),tonumber(p.y),tonumber(p.z)
      local key=nick:lower()
      if nick~='' and key~=tostring(shared.nick):lower() and x and y and z
          and math.abs(x)<5000 and math.abs(y)<5000 and math.abs(z)<5000
          and count<gps.max_markers then
        active[key]=true;count=count+1
        local old=gps.marks[key]
        -- Position must move each second; the GTA opcode has no setter for an existing blip.
        if old and ((x-old.x)^2+(y-old.y)^2>0.25 or math.abs(zbase-(old.baseZ or 0))>50) then
          if old.blip then pcall(removeBlip,old.blip) end
          gps.marks[key]=nil;old=nil
        end
        if not old then
          -- LOW relative to the *viewer* -> solid upward yellow triangle, black border.
          local decorativeZ=zbase-160.0
          local ok,b=pcall(addBlipForCoord,x,y,decorativeZ)
          if ok and b and b~=-1 then
            local markerColor, textColor=gpsNickColors(nick)
            pcall(changeBlipColour,b,markerColor) -- GTA custom RGBA color

            pcall(changeBlipDisplay,b,2) -- display on both radar & full map
            pcall(changeBlipScale,b,3)
            gps.marks[key]={blip=b,x=x,y=y,z=z,nick=nick,baseZ=zbase, color=textColor, cuffed=(p.cuffed==true), visibleColor=markerColor}
          end
        else
          old.nick=nick;old.z=z;old.cuffed=(p.cuffed==true)
          if not old.color then local _,color=gpsNickColors(nick); old.color=color end
        end
      end
    end
  end
  for key,entry in pairs(gps.marks) do
    if not active[key] then
      if entry.blip then pcall(removeBlip,entry.blip) end
      gps.marks[key]=nil
    end
  end
end
local function gpsCuffBlinkTick()
  if not gps.show then return end
  local red=0xFF2424FF -- GTA 0xRRGGBBAA bright red
  if red>=2147483648 then red=red-4294967296 end
  local phase=math.floor(getGameTimer()/500)%2
  for _,entry in pairs(gps.marks) do
    if entry.blip then
      local defaultColor=gpsNickColors(entry.nick)
      local selected=(gps.blink and entry.cuffed and phase==1) and red or defaultColor
      if selected~=entry.visibleColor then
        pcall(changeBlipColour,entry.blip,selected)
        entry.visibleColor=selected
      end
    end
  end
end

local function gpsTick()
  local t=getGameTimer()/1000
  -- Restart the independent worker if it never started or stopped unexpectedly.
  if bridgeReady and t-(gps.last_worker_check or 0)>8 then
    gps.last_worker_check=t
    local f=io.open(GPS_WORKER_ALIVE,'rb')
    local ts=nil
    if f then ts=tonumber(f:read('*a'));f:close() end
    if not ts or os.time()-ts>9 then gpsLaunch() end
  end
  if t-gps.written_at>=1 then
    gps.written_at=t
    -- Sending coordinates does not require GPS markers to be shown.
    local packet={share=gps.share,show=gps.show,cuffed=cuffDetect()}
    if gps.share then
      local ok,x,y,z=pcall(getCharCoordinates,PLAYER_PED)
      if ok and type(x)=='number' and type(y)=='number' and type(z)=='number' then
        packet.x=x; packet.y=y; packet.z=z
      else
        packet.share=false
      end
    end
    if bridgeReady then writeText(GPS_INPUT,json.encode(packet)) end
  end
  if not gps.show or not shared.connected then
    gpsRemoveMarkers()
    return
  end
  if t-gps.read_at>=0.2 then
    gps.read_at=t
    local f=io.open(GPS_STATE,'rb')
    if f then
      local raw=f:read('*a'); f:close()
      local ok,v=pcall(json.decode,raw)
      if ok and type(v)=='table' and type(v.at)=='number' and type(v.players)=='table' then
        if os.time()-v.at<=24 and v.at>=gps.received_at then
          gps.received_at=v.at
          gps.latest=v.players
          gpsApplyPositions(v.players)
        end
      end
    end
  end
  if gps.received_at>0 and os.time()-gps.received_at>24 then
    gpsRemoveMarkers()
  end
end
-- Notify /g once per genuine local cuff event, not from any chat message.
-- Existing cuffDetect() checks real SA-MP state / attachment RPC only.
local cuffAlert = {since_ms=nil, sent=false, next_try_ms=0}
local function cuffHelpTick()
  local now=getGameTimer()
  if not cuffDetect() then
    cuffAlert.since_ms=nil
    cuffAlert.sent=false
    cuffAlert.next_try_ms=0
    return
  end
  if not cuffAlert.since_ms then cuffAlert.since_ms=now end
  if cuffAlert.sent or now-cuffAlert.since_ms<900 or now<cuffAlert.next_try_ms then return end
  -- Silence errors when offline: no unwanted queue or repeated chat warnings.
  if not (bridgeReady and shared.connected and shared.nick~='') then return end
  cuffAlert.next_try_ms=now+5000
  local message=shared.nick..' в наручниках! Помогите ему — его метка находится на карте.'
  if sendMessage(message,1) then cuffAlert.sent=true end
end

local function gpsToggleShow()
  gps.show=not gps.show
  gps.written_at=-999
  if not gps.show then gpsRemoveMarkers() end
  gpsSavePrefs()
  sampAddChatMessage('[GPS] Map markers: '..(gps.show and 'ON' or 'OFF')..' (/gmap)',0x66CCFF)
end
local function gpsToggleShare()
  gps.share=not gps.share
  gps.written_at=-999
  gpsSavePrefs()
  sampAddChatMessage('[GPS] Share my location: '..(gps.share and 'ON' or 'OFF')..' (/gshare)',0x66CCFF)
end

-- Compact on-screen legend: all nick colors match the radar marker colors.
-- Not linked to network traffic, so the existing messenger remains untouched.
local function gpsRenderLegend()
  if not gps.show or not gps.legend or not shared.connected then return end
  if type(renderCreateFont)~='function' or type(renderFontDrawText)~='function' then return end
  if not toastFont then
    local ok,font=pcall(renderCreateFont,'Arial',10,5)
    if ok then toastFont=font end
  end
  if not toastFont then return end
  local playerList={}
  for _,entry in pairs(gps.marks) do
    if entry.nick then playerList[#playerList+1]=entry end
  end
  table.sort(playerList,function(a,b) return a.nick:lower()<b.nick:lower() end)
  if #playerList==0 then return end
  local w,h=1024,768
  if type(getScreenResolution)=='function' then
    local ok,ww,hh=pcall(getScreenResolution)
    if ok and type(ww)=='number' and type(hh)=='number' and ww>0 and hh>0 then w,h=ww,hh end
  end
  local x=math.max(12,w-254)
  local y=math.floor(h*0.29)
  drawOverlayLine('GPS PLAYERS ('..#playerList..')',x,y,0xFFFFFFFF)
  -- Fit the list on any screen resolution and avoid crowding the HUD.
  local visible=math.max(1,math.min(18,math.floor((h-y-45)/19)))
  for i=1,math.min(visible,#playerList) do
    local entry=playerList[i]
    local _,defaultColor=gpsNickColors(entry.nick)
    local blinkRed=(gps.blink and entry.cuffed and math.floor(getGameTimer()/500)%2==1)
    drawOverlayLine(utf8Short(entry.nick,25),x,y+6+i*19,blinkRed and 0xFFFF2424 or (entry.color or defaultColor))
  end
  if #playerList>visible then
    drawOverlayLine('+'..(#playerList-visible)..' more (/gmap list)',x,y+6+(visible+1)*19,0xFFE0E0E0)
  end
end

local function gpsToggleLegend()
  gps.legend=not gps.legend
  gpsSavePrefs()
  sampAddChatMessage('[GPS] Nicknames: '..(gps.legend and 'ON' or 'OFF')..' (/glegend)',0x66CCFF)
end

if hasImgui then
imgui.OnInitialize(function()
  imgui.GetIO().IniFilename = nil
  -- Russian help needs a font with Cyrillic glyphs; keep the original font for Messenger.
  local fontDirs = {}
  if type(getFolderPath) == 'function' then
    local ok, folder = pcall(getFolderPath, 0x14) -- CSIDL_FONTS
    if ok and type(folder) == 'string' and #folder > 0 then
      fontDirs[#fontDirs+1] = folder
    end
  end
  local windows = os.getenv('WINDIR') or 'C:\\Windows'
  fontDirs[#fontDirs+1] = windows .. '\\Fonts'
  for _,fontDir in ipairs(fontDirs) do
    for _,fontName in ipairs({'arial.ttf', 'tahoma.ttf', 'segoeui.ttf'}) do
      local fontPath = fontDir .. '\\' .. fontName
      if doesFileExist(fontPath) then
        local ok, font = pcall(function()
          local fonts = imgui.GetIO().Fonts
          return fonts:AddFontFromFileTTF(fontPath, 17, nil, fonts:GetGlyphRangesCyrillic())
        end)
        if ok and font ~= nil then helpCyrillicFont = font; break end
      end
    end
    if helpCyrillicFont then break end
  end
  scanBackgrounds()
end)

imgui.OnFrame(function() return window[0] end, function()
  imgui.SetNextWindowSize(imgui.ImVec2(850,570),imgui.Cond.FirstUseEver)
  imgui.Begin('SAMP Messenger 1.0.6 | Chat & Binds', window)
  imgui.Text('Online: '..tostring(#online)..' | Last entries in memory: '..tostring(#messages))
  if imgui.Button(gps.show and 'GPS map: ON' or 'GPS map: OFF') then gpsToggleShow() end
  imgui.SameLine()
  if imgui.Button(gps.share and 'Share position: ON' or 'Share position: OFF') then gpsToggleShare() end
  imgui.SameLine()
  if imgui.Button(gps.legend and 'Nickname list: ON' or 'Nickname list: OFF') then gpsToggleLegend() end
  imgui.SameLine()
  imgui.Text('Markers: '..tostring((function() local n=0; for _ in pairs(gps.marks) do n=n+1 end; return n end)()))
  if imgui.Button(gps.blink and 'Cuff blinking: ON' or 'Cuff blinking: OFF') then gps.blink=not gps.blink; gpsSavePrefs() end
  if imgui.Button('All messages') then active_tab=1; history_page=0 end
  imgui.SameLine()
  if imgui.Button('My messages (/ghistory)') then active_tab=2; history_page=0 end
  imgui.SameLine()
  if imgui.Button('Binds (/mbinds)') then active_tab=3 end
  imgui.Separator()
  if active_tab ~= 3 then
    imgui.Text('All messages are saved locally: moonloader/SAMP_Messenger/history_<nick>.jsonl')
    local filtered={}
    for _,m in ipairs(messages) do
      if active_tab==1 or m.me then filtered[#filtered+1]=m end
    end
    local pages=math.max(1,math.ceil(#filtered/80))
    if history_page>=pages then history_page=pages-1 end
    if imgui.Button('Older messages') and history_page<pages-1 then history_page=history_page+1 end
    imgui.SameLine()
    if imgui.Button('Newer messages') and history_page>0 then history_page=history_page-1 end
    imgui.SameLine()
    imgui.Text('Page '..(history_page+1)..' / '..pages)
    imgui.BeginChild('saved_messages',imgui.ImVec2(0,0),true)
    local last=#filtered-history_page*80
    local first=math.max(1,last-79)
    for i=first,last do
      local m=filtered[i]
      if m then
        local label=m.me and '[ME]' or m.nick
        local row='['..tostring(m.date or '')..'] '..label..': '..tostring(m.text or '')
        imgui.TextWrapped(row)
      end
    end
    if #filtered==0 then imgui.Text('No messages yet.') end
    imgui.EndChild()
  else
  imgui.BeginChild('bind_names',imgui.ImVec2(215,0),true)
  if imgui.Button('+ New bind',imgui.ImVec2(-1,32)) then
    persistEditor()
    binds[#binds+1]={name='New bind',key=0,ctrl=false,alt=false,shift=false,target=1,text=''}
    saveBinds(); selectBind(#binds)
  end
  imgui.Separator()
  for i,b in ipairs(binds) do
    if imgui.Selectable((b.name~='' and b.name or 'New bind')..'##bind'..i,selected_bind==i) then
      if selected_bind~=i then persistEditor() end
      selectBind(i)
    end
  end
  imgui.EndChild(); imgui.SameLine()
  imgui.BeginChild('bind_settings',imgui.ImVec2(0,0),true)
  if binds[selected_bind] then
    imgui.Text('Bind name:')
    if imgui.InputText('##name',bind_name,ffi.sizeof(bind_name)) then markEditorDirty() end
    imgui.Text('Hotkey: '..keyLabel({key=edit_key,ctrl=edit_ctrl,alt=edit_alt,shift=edit_shift}))
    if imgui.Button(capture and 'Recording... ESC cancel / DELETE clear' or 'Record key combination') then
      capture=true; key_preview='Press any keys'; capture_key=0; previous_keys={}
    end
    if capture then imgui.Text('Keys pressed: '..key_preview) end
    imgui.SameLine()
    if imgui.Button('Clear combination') then
      capture=false; edit_key=0; edit_ctrl=false; edit_alt=false; edit_shift=false; persistEditor()
    end
    if imgui.Button(bind_target==1 and 'Send to: /g Messenger' or 'Send to: SA:MP chat') then
      bind_target=3-bind_target; persistEditor()
    end
    local bind_chars = countUtf8Chars(ffi.string(bind_text))
    imgui.Text('Bind message ('..bind_chars..'/'..MAX_BIND_CHARS..' symbols):')
    if imgui.InputText('##bindtxt',bind_text,ffi.sizeof(bind_text)) then
      local current = ffi.string(bind_text):gsub('[\r\n]+', ' ')
      if countUtf8Chars(current) > MAX_BIND_CHARS then
        setBuffer(bind_text, cropUtf8(current, MAX_BIND_CHARS))
      elseif current ~= ffi.string(bind_text) then
        setBuffer(bind_text, current)
      end
      markEditorDirty()
    end
    if imgui.Button('Save bind') then persistEditor() end
    imgui.SameLine()
    if imgui.Button('Delete bind') then
      table.remove(binds,selected_bind); saveBinds()
      selected_bind=0; capture=false
    end
  else
    imgui.Text('Choose or create a bind on the left')
  end
  imgui.Separator()
  imgui.Text('Codes (automatically replaced when sending):')
  imgui.BeginChild('codes',imgui.ImVec2(0,0),true)
  local codes={
    {'{nick}','Your SA:MP nickname'}, {'{zone}','Current area'},
    {'{pos}','Current X, Y, Z'}, {'{x}','X coordinate'},
    {'{y}','Y coordinate'}, {'{z}','Z coordinate'},
    {'{hp}','Health'}, {'{armor}','Armour'},
    {'{online}','Messenger user count'}, {'{time}','Local time'}, {'{date}','Local date'}
  }
  for _, item in ipairs(codes) do imgui.Text(item[1]..'  -  '..item[2]) end
  imgui.EndChild()
  imgui.EndChild()
  end -- bind editor tab
  imgui.End()
end)

-- Separate help window: original Messenger UI, binds and networking are untouched.
local messengerHelpRows = {
  {'/g текст', 'Отправить сообщение игрокам Messenger и в Telegram-группу.'},
  {'/mess', 'Открыть окно переписки, истории и биндера.'},
  {'/ghistory', 'Открыть историю своих отправленных сообщений.'},
  {'/mbinds', 'Открыть редактор биндов и горячих клавиш.'},
  {'/gstatus', 'Проверить подключение к Messenger и сетевые ошибки.'},
  {'/gkey КОД', 'Сохранить общий код доступа на этом компьютере.'},
  {'/gcodec', 'Проверить отображение русских и украинских букв.'},
  {'/messhelp', 'Открыть это окно помощи по Messenger.'},
  {'/gpshelp', 'Открыть окно помощи по GPS.'},
}
local gpsHelpRows = {
  {'/gmap', 'Переключить отображение GPS-меток других игроков.'},
  {'/gmap on', 'Включить метки других игроков на карте.'},
  {'/gmap off', 'Скрыть метки других игроков на карте.'},
  {'/gmap list', 'Показать игроков и их последние координаты.'},
  {'/gmap status', 'Показать состояние GPS, количество игроков и ошибки.'},
  {'/gshare on', 'Разрешить другим игрокам видеть твоё местоположение.'},
  {'/gshare off', 'Запретить другим игрокам видеть твоё местоположение.'},
  {'/glegend on', 'Показать цветной список ников на экране.'},
  {'/glegend off', 'Скрыть список ников, оставив метки на карте.'},
  {'/gblink on', 'Включить мигание меток и ников при наручниках.'},
  {'/gblink off', 'Отключить мигание меток и ников.'},
  {'/gblink', 'Переключить мигание меток при наручниках.'},
  {'/gcuffstatus', 'Проверить определение наручников на твоём персонаже.'},
  {'/gpshelp', 'Открыть это окно помощи по GPS.'},
}
imgui.OnFrame(function() return helpWindow[0] end, function()
  imgui.SetNextWindowSize(imgui.ImVec2(710, 550), imgui.Cond.FirstUseEver)
  imgui.Begin('SAMP Messenger 1.1.2 | Help', helpWindow)
  if helpCyrillicFont then imgui.PushFont(helpCyrillicFont) end
  if imgui.Button('Мессенджер (/messhelp)', imgui.ImVec2(250, 33)) then helpSection=1 end
  imgui.SameLine()
  if imgui.Button('GPS (/gpshelp)', imgui.ImVec2(190, 33)) then helpSection=2 end
  imgui.Separator()
  local rows = helpSection==1 and messengerHelpRows or gpsHelpRows
  imgui.Text(helpSection==1 and 'Команды мессенджера' or 'Команды GPS и меток')
  imgui.Separator()
  imgui.BeginChild('SAMP_Messenger_Help_List', imgui.ImVec2(0, -47), true)
  for _,entry in ipairs(rows) do
    imgui.TextColored(imgui.ImVec4(0.36, 0.81, 1.0, 1.0), entry[1])
    imgui.TextWrapped(entry[2])
    imgui.Separator()
  end
  imgui.EndChild()
  if imgui.Button('Закрыть', imgui.ImVec2(125, 32)) then helpWindow[0]=false end
  if helpCyrillicFont then imgui.PopFont() end
  imgui.End()
end)

end -- optional mimgui UI

local function pollHotkeys()
  if capture then
    if wasKeyPressed(0x1B) then capture=false; return end
    if wasKeyPressed(0x2E) then
      capture=false; edit_key=0; edit_ctrl=false; edit_alt=false; edit_shift=false
      persistEditor(); return
    end
    local parts={}
    if anyCtrl() then parts[#parts+1]='CTRL' end
    if anyAlt() then parts[#parts+1]='ALT' end
    if anyShift() then parts[#parts+1]='SHIFT' end
    local key=0
    for _, code in ipairs(watch_keys) do
      if isKeyDown(code) then
        parts[#parts+1]=keynames[code] or tostring(code)
        if wasKeyPressed(code) then key=code end
      end
    end
    key_preview=#parts>0 and table.concat(parts,' + ') or 'Waiting for keys...'
    if key>0 then
      capture_key,capture_ctrl,capture_alt,capture_shift=key,anyCtrl(),anyAlt(),anyShift()
    end
    if capture_key>0 and #parts==0 then
      edit_key,edit_ctrl,edit_alt,edit_shift=capture_key,capture_ctrl,capture_alt,capture_shift
      capture=false; persistEditor()
    end
    return
  end
  if (getGameTimer()/1000)-last_hotkey<0.20 then return end
  if sampIsChatInputActive() or sampIsDialogActive() or window[0] or helpWindow[0] or isPauseMenuActive() then return end
  for _, code in ipairs(watch_keys) do
    if wasKeyPressed(code) then
      for _, b in ipairs(binds) do
        if b.key==code and b.ctrl==anyCtrl() and b.alt==anyAlt() and b.shift==anyShift() then
          sendMessage(expandCodes(b.text),b.target)
          last_hotkey=(getGameTimer()/1000)
          return
        end
      end
    end
  end
end

-- Help opens a graphical window; no command list is sent to SA:MP chat.
local function openHelp(section)
  if hasImgui then
    helpSection=section
    helpWindow[0]=true
  else
    sampAddChatMessage(safeChatText('[SAMP Messenger] Для окна помощи нужна библиотека mimgui.'), 0xFFCC66)
  end
end
local function showMessengerHelp() openHelp(1) end
local function showGpsHelp() openHelp(2) end

function main()
  repeat wait(250) until isSampAvailable()
  makeDir(); loadBinds(); scanBackgrounds(); gpsLoadPrefs()
  -- Register commands before any network setup.
  sampRegisterChatCommand('messhelp', showMessengerHelp)
  sampRegisterChatCommand('gpshelp', showGpsHelp)
  sampRegisterChatCommand('mess', function()
    if hasImgui then active_tab=1; history_page=0; window[0]=true else sampAddChatMessage('[Messenger] Install mimgui to view saved messages',0x66CCFF) end
  end)
  sampRegisterChatCommand('mbinds', function()
    if hasImgui then active_tab=3; window[0]=true else sampAddChatMessage('[Messenger] Install mimgui to use graphical bind editor',0x66CCFF) end
  end)
  sampRegisterChatCommand('ghistory', function()
    if hasImgui then
      active_tab=2; history_page=0; window[0]=true
    else
      local shown=0
      for i=#messages,1,-1 do
        local m=messages[i]
        if m.me then
          sampAddChatMessage(safeChatText('[My G] '..tostring(m.text):sub(1,120)),0x69B8DB)
          shown=shown+1
          if shown>=8 then break end
        end
      end
      if shown==0 then sampAddChatMessage('[G] No sent messages yet.',0x66CCFF) end
    end
  end)
  sampRegisterChatCommand('gmap', function(param)
    local s=tostring(param or ''):lower():match('^%s*(.-)%s*$')
    if s=='status' then
      local f=io.open(GPS_STATUS,'rb')
      if not f then sampAddChatMessage('[GPS] GPS worker has not responded; check /gstatus.',0xFF7777)
      else
        local raw=f:read('*a');f:close()
        local ok,p=pcall(json.decode,raw)
        if ok and type(p)=='table' then
          local age=os.time()-(tonumber(p.at) or 0)
          sampAddChatMessage(safeChatText('[GPS] '..(p.ok and 'ONLINE' or 'ERROR')..' | Seen: '..tostring(p.count or 0)..' | Age: '..tostring(age)..'s'),p.ok and 0xAAFF99 or 0xFF8888)
          if p.error and p.error~='' then sampAddChatMessage(safeChatText('[GPS] '..tostring(p.error):sub(1,155)),0xFF8888) end
        else sampAddChatMessage('[GPS] status file unreadable; retry.',0xFF8888) end
      end
      sampAddChatMessage('[GPS] Show: '..tostring(gps.show)..' | Share: '..tostring(gps.share),0x66CCFF)
    elseif s=='list' then
      local n=0
      for _,item in pairs(gps.marks) do
        n=n+1
        sampAddChatMessage('[GPS] '..item.nick..': '..math.floor(item.x)..', '..math.floor(item.y),0x66CCFF)
      end
      if n==0 then sampAddChatMessage('[GPS] No active markers; check /gshare with friends.',0x66CCFF) end
    elseif s=='on' or s=='off' then
      gps.show=s=='on'
      gps.written_at=-999
      if not gps.show then gpsRemoveMarkers() end
      gpsSavePrefs()
      sampAddChatMessage('[GPS] Map markers: '..(gps.show and 'ON' or 'OFF'),0x66CCFF)
    else
      gpsToggleShow()
    end
  end)
  sampRegisterChatCommand('glegend', function(param)
    local s=tostring(param or ''):lower():match('^%s*(.-)%s*$')
    if s=='on' or s=='off' then
      gps.legend=(s=='on')
      gpsSavePrefs()
      sampAddChatMessage('[GPS] Nicknames: '..(gps.legend and 'ON' or 'OFF'),0x66CCFF)
    else gpsToggleLegend() end
  end)
  sampRegisterChatCommand('gshare', function(param)
    local s=tostring(param or ''):lower():match('^%s*(.-)%s*$')
    if s=='on' or s=='off' then
      gps.share=s=='on'
      gps.written_at=-999
      gpsSavePrefs()
      sampAddChatMessage('[GPS] Share my location: '..(gps.share and 'ON' or 'OFF'),0x66CCFF)
    else gpsToggleShare() end
  end)
  sampRegisterChatCommand('gblink', function(param)
    local s=tostring(param or ''):lower():match('^%s*(.-)%s*$')
    if s=='on' or s=='off' then gps.blink=(s=='on')
    else gps.blink=not gps.blink end
    gpsSavePrefs()
    sampAddChatMessage('[GPS] Handcuff blinking: '..(gps.blink and 'ON' or 'OFF'),0x66CCFF)
  end)
  sampRegisterChatCommand('gcuffstatus', function()
    local current=cuffDetect()
    local n=0; for _ in pairs(cuffEvidence.attached) do n=n+1 end
    sampAddChatMessage('[GPS] Real cuff signal: '..(current and 'YES' or 'NO')..' | SpecialAction24: '..tostring(cuffEvidence.special)..' | CuffObjects: '..tostring(n),0x66CCFF)
    sampAddChatMessage('[GPS] Detection uses SA-MP state/RPC, NOT chat text.',0x66CCFF)
  end)
  sampRegisterChatCommand('gstatus', function()
    local msg = shared.connected and 'ONLINE' or 'OFFLINE'
    sampAddChatMessage('[G] Status: ' .. msg .. ' | ' .. tostring(shared.error):sub(1,85), 0x66CCFF)
    local f = io.open(NETLOG, 'rb')
    if f then
      local raw = f:read('*a') or ''; f:close()
      local last = raw:match('([^\r\n]+)\r?\n?$') or raw:sub(-200)
      sampAddChatMessage(safeChatText('[G] Network: ' .. last:sub(1,125)), 0x66CCFF)
    else
      sampAddChatMessage('[G] No network.log. PowerShell may not have started.', 0xFF7777)
      local lf = io.open(LAUNCHLOG, 'rb')
      if lf then
        local raw = lf:read('*a') or ''; lf:close()
        local result = raw:match('ShellExecuteA result: (%d+)') or '?'
        sampAddChatMessage('[G] Launcher ShellExecuteA result: ' .. result .. ' (see launch.log)', 0xFFCC66)
      end
    end
  end)
  sampRegisterChatCommand('gcodec', function()
    local sample = safeChatText('Проверка кодировки / Привіт')
    sampAddChatMessage('[G] Local UTF-8 test: ' .. sample, 0x66CCFF)
    sampAddChatMessage('[G] If this is readable, the in-game CP1251 output works.', 0x66CCFF)
  end)
  -- A shared access code is saved once per PC, for ALL SA:MP accounts.
  -- This is a local command. It is not transmitted to in-game chat or /g.
  sampRegisterChatCommand('gkey', function(param)
    local key=tostring(param or ''):match('^%s*(.-)%s*$')
    if #key<8 or #key>128 or not key:match('^[A-Za-z0-9_%-]+$') then
      sampAddChatMessage('[G] Usage: /gkey ACCESS_CODE (8-128 letters/digits)',0xFF8888)
      return
    end
    makeDir()
    local keyFile=DIR..'access_code.txt'
    if writeText(keyFile,key) then
      sampAddChatMessage('[G] Access code saved on this PC. Connecting...',0x99FF99)
      -- The background worker re-reads the file on each reconnect attempt.
    else
      sampAddChatMessage('[G] Cannot save access code (moonloader/SAMP_Messenger folder).',0xFF8888)
    end
  end)
  sampRegisterChatCommand('g', function(param)
    local txt = tostring(param or ''):match('^%s*(.-)%s*$')
    if txt == '' then
      sampAddChatMessage('[G] Usage: /g message | /mbinds - hotkeys | /mess - window',0x66CCFF)
      return
    end
    local ok, utftext = pcall(gameInputToUtf8, txt)
    sendMessage(expandCodes(ok and utftext or txt), 1)
  end)
  sampAddChatMessage(safeChatText('[SAMP Messenger] 1.1.9 (Помощь /gpshelp, /messhelp; код /gkey)'),0x66CCFF)
  checkAutoUpdate()
  local ok,id = sampGetPlayerIdByCharHandle(PLAYER_PED)
  if ok then
    shared.nick=sampGetPlayerNickname(id)
    loadHistory(shared.nick)
    local started, reason = launchNetwork(shared.nick)
    if not started then status = reason; shared.error = reason end
  else status='SA:MP nickname unavailable; /mess still works' end
  while true do
    wait(0)
    pollHotkeys()
    networkTick()
    gpsTick()
    gpsCuffBlinkTick()
    cuffHelpTick()
    checkAutoUpdate()
    renderToast()
    gpsRenderLegend()
    if editor_dirty and (getGameTimer()/1000)-save_clock > 0.65 then persistEditor() end
    if shared.connected then
      status='Connected: '..tostring(shared.nick)
      if shared.error~='' then status=status..' | '..tostring(shared.error) end
    else
      status='Offline: '..tostring(shared.error)
    end
    if shared.output~='' then
      local raw=shared.output; shared.output=''
      local good,data=pcall(json.decode,raw)
      if good and type(data)=='table' then
        online=data.online or online
        for _,entry in ipairs(data.messages or {}) do addMessage(entry) end
      end
    end
  end
end
function onScriptTerminate(script, quitGame)
  if script==thisScript() then gpsRemoveMarkers(); cuffResetLocal(); gpsSavePrefs(); persistEditor(); shared.running=false; os.remove(HEARTBEAT) end
end
