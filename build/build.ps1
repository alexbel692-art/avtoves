# Сборка установщика АвтоВес для Windows.
#   powershell -ExecutionPolicy Bypass -File build\build.ps1 -Version 1.0.0
# Результат: dist\AvtoVes-Setup-<версия>.exe
#
# Что делает:
#  1. Встроенный Python 3.12 (python.org, embeddable) — на весовой Python ставить не нужно.
#  2. Все библиотеки (pip, только готовые сборки под Windows x64).
#  3. Модели распознавания номеров — скачиваются сейчас и кладутся внутрь (работа без интернета).
#  4. Проверка: распознавание запускается, полный проезд в режиме имитации проходит.
#  5. WinSW — обёртка службы Windows.
#  6. Inno Setup собирает установщик.
param(
  [string]$Version = "1.0.0",
  [switch]$NoTest
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root  = Split-Path -Parent $PSScriptRoot
$Stage = Join-Path $Root "build\stage"
$Cache = Join-Path $Root "build\cache"
$Dist  = Join-Path $Root "dist"

$PyVer      = "3.12.10"
$PyZipUrl   = "https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-amd64.zip"
$PySha256   = $env:PY_EMBED_SHA256      # если задан — сверяем
$WinSWTag   = "v2.12.0"
$WinSWUrl   = "https://github.com/winsw/winsw/releases/download/$WinSWTag/WinSW-x64.exe"
$WinSWSha256 = $env:WINSW_SHA256

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Fail($t) { Write-Host "ОШИБКА: $t" -ForegroundColor Red; exit 1 }

function Get-File($Url, $Out, $Sha) {
  if (-not (Test-Path $Out)) {
    Write-Host "   скачиваю $Url"
    Invoke-WebRequest -Uri $Url -OutFile "$Out.part" -UseBasicParsing
    Move-Item "$Out.part" $Out -Force
  }
  $h = (Get-FileHash $Out -Algorithm SHA256).Hash.ToLower()
  Write-Host "   SHA256 $h  $(Split-Path -Leaf $Out)"
  if ($Sha -and $h -ne $Sha.ToLower()) { Remove-Item $Out; Fail "$(Split-Path -Leaf $Out): контрольная сумма не совпала" }
  return $h
}

# ---------------------------------------------------------------- Python для сборки (pip)
Step "Python для сборки"
$HostExe = $null; $HostArgs = @()
foreach ($c in @("py -3.12", "py -3", "python")) {
  $parts = $c.Split(" ")
  $exe = $parts[0]; $pre = @($parts | Select-Object -Skip 1)
  if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
  try {
    $v = & $exe @pre -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
    if ($LASTEXITCODE -eq 0 -and [int]$v -ge 310) { $HostExe = $exe; $HostArgs = $pre; break }
  } catch {}
}
if (-not $HostExe) { Fail "Не найден Python 3.10+ (нужен только для сборки). Установите: winget install Python.Python.3.12" }
Write-Host "   $HostExe $HostArgs"
function HostPy { & $HostExe @HostArgs @args; if ($LASTEXITCODE -ne 0) { Fail "python $args" } }

# ---------------------------------------------------------------- подготовка папок
Step "Подготовка"
New-Item -ItemType Directory -Force -Path $Cache, $Dist | Out-Null
if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

# ---------------------------------------------------------------- встроенный Python
Step "Встроенный Python $PyVer"
$pyZip = Join-Path $Cache "python-$PyVer-embed-amd64.zip"
Get-File $PyZipUrl $pyZip $PySha256 | Out-Null
$PyDir = Join-Path $Stage "python"
Expand-Archive $pyZip -DestinationPath $PyDir -Force
$pth = Get-ChildItem $PyDir -Filter "python3*._pth" | Select-Object -First 1
if (-not $pth) { Fail "в архиве Python нет файла ._pth" }
$zipName = ($pth.BaseName) + ".zip"
# ".." — папка программы (main.py и пакет app), Lib\site-packages — библиотеки
Set-Content -Path $pth.FullName -Encoding ASCII -Value @($zipName, ".", "Lib\site-packages", "..", "import site")
# библиотеки Visual C++ — чтобы работало на «чистой» Windows / Windows Server (как в Ласточке)
foreach ($f in "msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll") {
  $src = Join-Path $env:WINDIR "System32\$f"
  if ((Test-Path $src) -and -not (Test-Path (Join-Path $PyDir $f))) { Copy-Item $src $PyDir }
}

# ---------------------------------------------------------------- библиотеки
Step "Библиотеки (pip)"
$Site = Join-Path $PyDir "Lib\site-packages"
HostPy -m pip install --disable-pip-version-check --no-warn-script-location `
  --target $Site --platform win_amd64 --python-version 3.12 --implementation cp `
  --only-binary=:all: --upgrade -r (Join-Path $Root "requirements.txt")
Get-ChildItem $Site -Directory -Filter "bin" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# ---------------------------------------------------------------- код программы
Step "Код АвтоВес $Version"
Copy-Item (Join-Path $Root "main.py") $Stage
Copy-Item (Join-Path $Root "README.md") $Stage
Copy-Item (Join-Path $Root "app") (Join-Path $Stage "app") -Recurse
Get-ChildItem (Join-Path $Stage "app") -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Set-Content -Path (Join-Path $Stage "app\__init__.py") -Encoding ASCII -Value "__version__ = `"$Version`""
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "1C") | Out-Null
Copy-Item (Join-Path $Root "1c\*") (Join-Path $Stage "1C")
foreach ($f in "config.template.yaml", "AvtoVesService.xml", "avtoves.ico") {
  Copy-Item (Join-Path $Root "installer\$f") $Stage
}
$Py = Join-Path $PyDir "python.exe"

# ---------------------------------------------------------------- модели распознавания
Step "Модели распознавания номеров"
$Models = Join-Path $Stage "models"
New-Item -ItemType Directory -Force -Path $Models | Out-Null
$oldUP = $env:USERPROFILE; $oldHome = $env:HOME
$env:USERPROFILE = $Models; $env:HOME = $Models
$probe = @'
import numpy as np, cv2
from fast_alpr import ALPR
a = ALPR(detector_model="yolo-v9-t-384-license-plate-end2end", ocr_model="cct-xs-v1-global-model")
img = np.full((720, 1280, 3), 90, np.uint8)
cv2.rectangle(img, (500, 400), (780, 460), (255, 255, 255), -1)
cv2.putText(img, "A123BC77", (510, 448), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3)
r = a.predict(img)
print("распознавание работает, найдено номеров:", len(r), [x.ocr.text for x in r if x.ocr])
'@
$probeFile = Join-Path $Cache "probe_alpr.py"
Set-Content -Path $probeFile -Encoding UTF8 -Value $probe
& $Py -X utf8 $probeFile
$rc = $LASTEXITCODE
$env:USERPROFILE = $oldUP; $env:HOME = $oldHome
if ($rc -ne 0) { Fail "распознавание номеров не запустилось" }
$mcount = (Get-ChildItem $Models -Recurse -File | Measure-Object).Count
if ($mcount -lt 2) { Fail "модели не скачались в $Models" }
Get-ChildItem $Models -Recurse -File | ForEach-Object { Write-Host "   $($_.FullName.Substring($Models.Length)) $([math]::Round($_.Length/1MB,1)) МБ" }

# ---------------------------------------------------------------- проверка программы
if (-not $NoTest) {
  Step "Проверка: проезд машины в режиме имитации"
  $T = Join-Path $Cache "smoke"
  if (Test-Path $T) { Remove-Item $T -Recurse -Force }
  New-Item -ItemType Directory -Force -Path $T | Out-Null
  & $Py -X utf8 (Join-Path $Stage "app\setup_config.py") --template (Join-Path $Stage "config.template.yaml") `
      --out (Join-Path $T "config.yaml") --simulate --web-port 18080
  if ($LASTEXITCODE -ne 0) { Fail "setup_config" }
  $p = Start-Process -FilePath $Py -ArgumentList "-X", "utf8", "`"$(Join-Path $Stage 'main.py')`"", "--config", "`"$(Join-Path $T 'config.yaml')`"" `
        -PassThru -WindowStyle Hidden -RedirectStandardOutput (Join-Path $T "out.log") -RedirectStandardError (Join-Path $T "err.log")
  try {
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
      Start-Sleep -Milliseconds 500
      try { $s = Invoke-RestMethod "http://127.0.0.1:18080/api/state"; $ok = $true; break } catch {}
    }
    if (-not $ok) { Get-Content (Join-Path $T "err.log") -Tail 30; Fail "программа не запустилась" }
    if (-not $s.scale.connected) { Fail "нет связи с имитатором весов" }
    $body = '{"plate":"А123ВС77","weight":25000,"side":"a"}'
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:18080/api/sim/drive" -ContentType "application/json; charset=utf-8" `
      -Body ([Text.Encoding]::UTF8.GetBytes($body)) | Out-Null
    $trips = @()
    for ($i = 0; $i -lt 60; $i++) {
      Start-Sleep -Seconds 1
      $resp = Invoke-RestMethod "http://127.0.0.1:18080/api/trips"; $trips = @($resp)
      if ($trips.Count -ge 1) { break }
    }
    if ($trips.Count -lt 1) { Fail "взвешивание не записалось" }
    Write-Host "   записан рейс: $($trips[0].plate) $($trips[0].w1) кг" -ForegroundColor Green
  } finally {
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }
  }
}

# ---------------------------------------------------------------- служба
Step "WinSW $WinSWTag (служба Windows)"
$ws = Join-Path $Cache "WinSW-x64-$WinSWTag.exe"
$h = Get-File $WinSWUrl $ws $WinSWSha256
try {
  $rel = Invoke-RestMethod "https://api.github.com/repos/winsw/winsw/releases/tags/$WinSWTag" -Headers @{ "User-Agent" = "avtoves-build" }
  $d = ($rel.assets | Where-Object { $_.name -eq "WinSW-x64.exe" }).digest
  if ($d -and $d -ne "sha256:$h") { Remove-Item $ws; Fail "WinSW: $h, а GitHub указывает $d" }
  if ($d) { Write-Host "   совпадает с отпечатком GitHub" }
} catch { Write-Host "   (отпечаток GitHub проверить не удалось: $_)" }
Copy-Item $ws (Join-Path $Stage "AvtoVesService.exe")

# ---------------------------------------------------------------- Inno Setup
Step "Установщик (Inno Setup)"
$iscc = $null
foreach ($c in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                 "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) { if (Test-Path $c) { $iscc = $c; break } }
if (-not $iscc) {
  Write-Host "   Inno Setup не найден — устанавливаю"
  if (Get-Command choco -ErrorAction SilentlyContinue) { choco install innosetup -y --no-progress | Out-Null }
  elseif (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
  }
  foreach ($c in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                   "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) { if (Test-Path $c) { $iscc = $c; break } }
}
if (-not $iscc) { Fail "Установите Inno Setup 6: https://jrsoftware.org/isdl.php" }
& $iscc /Q "/DAppVer=$Version" (Join-Path $Root "installer\avtoves.iss")
if ($LASTEXITCODE -ne 0) { Fail "Inno Setup не собрал установщик" }

$exe = Join-Path $Dist "AvtoVes-Setup-$Version.exe"
$hh = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
Set-Content -Path (Join-Path $Dist "SHA256SUMS.txt") -Encoding ASCII -Value "$hh  AvtoVes-Setup-$Version.exe"
Step "Готово"
Write-Host "   $exe  ($([math]::Round((Get-Item $exe).Length/1MB,1)) МБ)" -ForegroundColor Green
