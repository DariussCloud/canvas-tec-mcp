param([switch]$SkipLogin)

function Ok([string]$Msg, [string]$Detail = "") {
  Write-Host "✓ $Msg " -ForegroundColor Green -NoNewline
  Write-Host $Detail -ForegroundColor DarkGray
}
function Info([string]$Msg) { Write-Host $Msg -ForegroundColor DarkGray }
function Fail([string]$Msg) { Write-Host "✗ $Msg" -ForegroundColor Red; exit 1 }

$Src = Join-Path $PSScriptRoot "canvas_tec_mcp.py"
$DestDir = Join-Path $HOME "mcp\canvas-tec"
$Dest = Join-Path $DestDir "canvas_tec_mcp.py"
$BaseUrl = if ($env:CANVAS_BASE_URL) { $env:CANVAS_BASE_URL } else { "https://experiencia21.tec.mx" }

Write-Host "canvas-tec MCP " -ForegroundColor Cyan -NoNewline
Info "→ $BaseUrl"

$UvCmd = Get-Command uv -ErrorAction SilentlyContinue
$UvLocal = Join-Path $HOME ".local\bin\uv.exe"
if (-not $UvCmd -and -not (Test-Path $UvLocal)) {
  Info "Instalando uv…"
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
}
$Uv = if ($UvCmd) { $UvCmd.Source } else { $UvLocal }
if (-not (Test-Path $Uv)) { Fail "No se encontró uv después de instalarlo" }
Ok "uv" $Uv

New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
Copy-Item $Src $Dest -Force
Ok "Script" $Dest

Info "Preparando dependencias de Python…"
& $Uv sync --script $Dest --quiet
if ($LASTEXITCODE -ne 0) { Fail "No se pudieron instalar las dependencias" }
Ok "Dependencias"

$Pw = (Select-String -Path $Src -Pattern 'playwright==[0-9.]+' | Select-Object -First 1).Matches[0].Value
Info "Instalando Chromium de Playwright ($Pw)…"
& $Uv run --with $Pw playwright install chromium | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "No se pudo instalar Chromium" }
Ok "Chromium" (Join-Path $env:LOCALAPPDATA "ms-playwright")

$Configs = @(Join-Path $env:APPDATA "Claude\claude_desktop_config.json")
$Package = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Packages") -Directory -Filter "Claude_*" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($Package) { $Configs += Join-Path $Package.FullName "LocalCache\Roaming\Claude\claude_desktop_config.json" }

$Py = @'
import json, sys
from pathlib import Path
cfg_path, uv, script, base = sys.argv[1:5]
p = Path(cfg_path)
p.parent.mkdir(parents=True, exist_ok=True)
raw = p.read_text(encoding="utf-8-sig") if p.exists() else ""
cfg = json.loads(raw) if raw.strip() else {}
cfg.setdefault("mcpServers", {})["canvas-tec"] = {
    "command": uv,
    "args": ["run", "--script", script],
    "env": {"CANVAS_BASE_URL": base},
}
p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
'@
foreach ($Config in $Configs) {
  if (Test-Path $Config) { Copy-Item $Config "$Config.bak" -Force }
  $Py | & $Uv run --no-project python - $Config $Uv $Dest $BaseUrl
  if ($LASTEXITCODE -ne 0) { Fail "No se pudo escribir $Config" }
  Ok "claude_desktop_config.json" "$Config (respaldo en .bak)"
}

if ($SkipLogin) {
  Info "Login omitido. Cuando quieras: uv run --script `"$Dest`" --login"
  exit 0
}

$env:CANVAS_BASE_URL = $BaseUrl
Write-Host "Se abrirá Chromium: " -NoNewline
Write-Host "inicia sesión con tu cuenta del Tec (SSO + MFA)."
& $Uv run --script $Dest --login
if ($LASTEXITCODE -ne 0) { Fail "No se completó el login. Vuelve a correr install.ps1." }

& $Uv run --script $Dest --test
if ($LASTEXITCODE -ne 0) { Fail "La prueba falló. Revisa la sesión o CANVAS_BASE_URL." }

Write-Host "Listo. " -ForegroundColor Green -NoNewline
Write-Host "Cierra Claude Desktop por completo (clic derecho en su ícono de la bandeja → Quit) y vuelve a abrirlo."
