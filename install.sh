#!/usr/bin/env bash
set -euo pipefail

G=$'\033[32m'; R=$'\033[31m'; C=$'\033[36m'; D=$'\033[2m'; B=$'\033[1m'; N=$'\033[0m'
SRC="$(cd "$(dirname "$0")" && pwd)/canvas_tec_mcp.py"
DEST_DIR="$HOME/mcp/canvas-tec"
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
BASE_URL="${CANVAS_BASE_URL:-https://experiencia21.tec.mx}"

echo "${B}${C}canvas-tec MCP${N} ${D}→ $BASE_URL${N}"

if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  echo "${D}Instalando uv…${N}"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
echo "${G}✓ uv${N} ${D}$UV${N}"

mkdir -p "$DEST_DIR"
cp "$SRC" "$DEST_DIR/"
echo "${G}✓ Script${N} ${D}$DEST_DIR/canvas_tec_mcp.py${N}"

PW="$(grep -o 'playwright==[0-9.]*' "$SRC" | head -1)"
echo "${D}Instalando Chromium de Playwright ($PW)…${N}"
"$UV" run --with "$PW" playwright install chromium >/dev/null
echo "${G}✓ Chromium${N} ${D}~/Library/Caches/ms-playwright${N}"

mkdir -p "$(dirname "$CONFIG")"
[ -f "$CONFIG" ] && cp "$CONFIG" "$CONFIG.bak"
"$UV" run --no-project python - "$CONFIG" "$UV" "$DEST_DIR/canvas_tec_mcp.py" "$BASE_URL" <<'PY'
import json, sys
from pathlib import Path
cfg_path, uv, script, base = sys.argv[1:5]
p = Path(cfg_path)
raw = p.read_text() if p.exists() else ""
cfg = json.loads(raw) if raw.strip() else {}
cfg.setdefault("mcpServers", {})["canvas-tec"] = {
    "command": uv,
    "args": ["run", "--script", script],
    "env": {"CANVAS_BASE_URL": base},
}
p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
PY
echo "${G}✓ claude_desktop_config.json${N} ${D}(respaldo en .bak)${N}"

echo "${B}Se abrirá Chromium:${N} inicia sesión con tu cuenta del Tec (SSO + MFA)."
if ! CANVAS_BASE_URL="$BASE_URL" "$UV" run --script "$DEST_DIR/canvas_tec_mcp.py" --login; then
  echo "${R}✗ No se completó el login.${N} Vuelve a correr install.sh."
  exit 1
fi

if CANVAS_BASE_URL="$BASE_URL" "$UV" run --script "$DEST_DIR/canvas_tec_mcp.py" --test; then
  echo "${B}${G}Listo.${N} Cierra Claude Desktop con ⌘Q y vuelve a abrirlo."
else
  echo "${R}✗ La prueba falló.${N} Revisa la sesión o CANVAS_BASE_URL."
  exit 1
fi
