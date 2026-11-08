# canvas-tec MCP

Servidor MCP local que conecta Claude con Canvas (Experiencia21, Tec de Monterrey). El Tec no permite que los estudiantes generen tokens de API, así que el servidor usa tu propia sesión web, igual que el navegador. Claude ve exactamente lo que tú ves como estudiante.

> **Aviso.** Proyecto personal, no oficial y sin relación con el Tecnológico de Monterrey ni con Instructure. Automatiza tu propia sesión de Experiencia21; revisa el reglamento de uso de TI del Tec antes de usarlo y úsalo bajo tu responsabilidad. Cada persona inicia sesión con su propia cuenta: el repositorio no contiene credenciales.

Requisitos: macOS o Windows 10/11, [Claude Desktop](https://claude.ai/download) y una cuenta del Tec con acceso a Experiencia21. `uv` y Chromium se instalan solos.

## Cómo funciona

- Es un script local (stdio). Claude Desktop lo arranca al abrirse y lo cierra al salir: no hay servidor ni puerto que mantener.
- `uv` instala las dependencias de forma automática la primera vez (vienen declaradas en la cabecera del script).
- **Autenticación por sesión** (default si no hay `CANVAS_API_TOKEN`):
  1. `--login` abre Chromium (perfil propio en `~/.config/canvas-tec/browser-profile`). Inicias sesión a mano con SSO del Tec + MFA; tu contraseña nunca pasa por el script.
  2. Al detectar que cargó Experiencia21, guarda las cookies de `experiencia21.tec.mx` y las del IdP del Tec (`amfs.tec.mx`, `aamfa.tec.mx`) en `~/.config/canvas-tec/session.json` **cifrado**: en macOS con una llave que vive en el Llavero (servicio `canvas-tec-session`) y permisos 600; en Windows con DPAPI, que lo amarra a tu usuario de Windows.
  3. Cada petición manda la cookie `canvas_session`; en POST/PUT/DELETE agrega `X-CSRF-Token` con el `_csrf_token` vigente (Canvas lo renueva en cada respuesta).
  4. Si Canvas responde 401 o redirige al login, el servidor intenta **renovar en silencio**: abre Chromium headless con las cookies guardadas del IdP y, si la sesión del Tec sigue viva, obtiene una sesión nueva de Canvas sin MFA. Si no se puede, la herramienta responde `Sesión expirada: corre uv run --script …/canvas_tec_mcp.py --login`.
- Máximo 5 peticiones por segundo y espera progresiva ante 429 o "Rate Limit Exceeded".

## 1. Instalar

### macOS

```bash
git clone https://github.com/DariussCloud/canvas-tec-mcp.git
cd canvas-tec-mcp
bash install.sh
```

El instalador:
1. instala `uv` si falta;
2. copia el script a `~/mcp/canvas-tec/`;
3. instala el Chromium de Playwright (`uv run --with playwright==… playwright install chromium`);
4. agrega `canvas-tec` a `claude_desktop_config.json` (respaldo `.bak`);
5. corre `--login`: se abre Chromium e inicias sesión (SSO + MFA);
6. corre `--test`: valida la sesión y lista tus cursos y pendientes.

Después cierra Claude Desktop con **⌘Q** y vuelve a abrirlo.

### Windows

En PowerShell (no hace falta abrirlo como administrador):

```powershell
git clone https://github.com/DariussCloud/canvas-tec-mcp.git
cd canvas-tec-mcp
powershell -ExecutionPolicy Bypass -File install.ps1
```

Si no tienes git, descarga el ZIP desde **Code → Download ZIP**, descomprímelo y corre la última línea dentro de la carpeta.

`install.ps1` hace lo mismo que `install.sh`: instala `uv` y Chromium, copia el script a `%USERPROFILE%\mcp\canvas-tec\`, registra `canvas-tec` en `%APPDATA%\Claude\claude_desktop_config.json` (y en la copia de la versión de Microsoft Store, si existe), corre `--login` y `--test`. Con `-SkipLogin` solo instala.

Después cierra Claude Desktop por completo (clic derecho en su ícono de la bandeja → **Quit**) y vuelve a abrirlo.

## 2. Cuando la sesión caduca

Las cookies del Tec y de Canvas son de sesión: no tienen fecha fija y el servidor las invalida tras un tiempo. Mientras la sesión del IdP del Tec siga viva, el MCP se renueva solo. Cuando también vence la del IdP, Claude te mostrará:

```
Sesión expirada: corre `uv run --script ~/mcp/canvas-tec/canvas_tec_mcp.py --login`
```

El mensaje trae la ruta exacta de tu instalación (en Windows, `C:\Users\<tú>\mcp\canvas-tec\canvas_tec_mcp.py`). Córrelo en Terminal o PowerShell e inicia sesión en la ventana que se abre. No hace falta reiniciar Claude Desktop: en la siguiente petición el servidor detecta el `session.json` nuevo y lo usa. Si la sesión del IdP aún sirve, `--login` entra solo sin pedir MFA.

Comprobar en cualquier momento:

```bash
uv run --script ~/mcp/canvas-tec/canvas_tec_mcp.py --test
```

```powershell
uv run --script "$HOME\mcp\canvas-tec\canvas_tec_mcp.py" --test
```

O pregúntale a Claude por `session_status`.

## Variables opcionales (bloque `env` de la config)

| Variable | Default | Uso |
|---|---|---|
| `CANVAS_BASE_URL` | `https://experiencia21.tec.mx` | Dominio de Canvas |
| `CANVAS_API_TOKEN` | — | Si existe (o, en macOS, hay token en el Llavero, servicio `canvas-tec`), usa token en vez de sesión |
| `CANVAS_AUTH` | automático | `session` fuerza el modo sesión aunque haya token |
| `CANVAS_CONFIG_DIR` | `~/.config/canvas-tec` | Perfil del navegador y `session.json` |
| `CANVAS_SESSION_STORE` | cifrado | `file` guarda `session.json` sin cifrar |
| `CANVAS_IDP_HOSTS` | `amfs.tec.mx,aamfa.tec.mx` | Dominios del SSO cuyas cookies se guardan para renovar |
| `CANVAS_MAX_RPS` | `5` | Límite de peticiones por segundo |
| `CANVAS_DOWNLOADS` | `~/Downloads/Canvas` | Carpeta de descargas (subcarpeta por curso) |
| `CANVAS_TZ` | `America/Mexico_City` | Zona horaria de las fechas |
| `CANVAS_MAX_TEXT` | `60000` | Caracteres por bloque de texto |

## Herramientas

**Lectura**
- `session_status` (¿sesión viva?, a nombre de quién, cuándo se inició)
- `whoami`, `list_courses`, `get_course` (temario y pestañas)
- `list_modules` (todos los elementos y su estado de completado)
- `list_pages`, `get_page`
- `list_assignments`, `get_assignment` (instrucciones, rúbrica, entrega y comentarios)
- `list_announcements` (todos los cursos o uno)
- `list_discussions`, `get_discussion` (hilo completo)
- `list_files`, `download_file` (descarga y extrae texto de PDF, PPTX con notas del orador, DOCX, código; PPT/DOC si tienes LibreOffice)
- `read_local_file`
- `get_grades` (por curso o por tarea), `get_todo` (agenda de todos los cursos), `list_quizzes`
- `open_url`: pega cualquier enlace de Experiencia21 y lo abre
- `canvas_get`: GET genérico a cualquier endpoint de la API

**Escritura** (Claude pide confirmación antes)
- `submit_assignment`: archivos, texto o URL, con comentario opcional
- `post_discussion_reply`

## Ejemplos

- "¿Qué tengo pendiente esta semana?"
- "Descarga las presentaciones del módulo 5 de Física y hazme un resumen."
- "Abre este link y dime qué piden: https://experiencia21.tec.mx/courses/…/assignments/…"
- "Entrega ~/Documents/reporte.pdf en la tarea Reporte 3 de Química."

## Límites

- Las herramientas externas (LTI) como Zoom, editoriales, Google Slides embebidos o New Quizzes por dentro no salen en la API. `get_page` muestra su URL como `[contenido embebido: …]`; para abrirlas hay que usar el navegador.
- Las presentaciones escaneadas o hechas solo de imágenes casi no tienen texto. En ese caso la herramienta avisa con `note` y Claude abre el archivo descargado.
- No responde quizzes por ti.

## Seguridad

- `session.json` equivale a tu sesión iniciada: quien lo tenga y pueda descifrarlo (llave del Llavero en macOS, tu usuario de Windows con DPAPI) puede actuar como tú en Canvas hasta que la sesión caduque. Está en `~/.config/canvas-tec/` (en Windows, `%USERPROFILE%\.config\canvas-tec\`), cifrado y fuera de cualquier repositorio.
- La salida de `--login`, `--test` y los errores solo muestran los últimos 4 caracteres de cada cookie.
- Para cerrar la sesión del MCP: `rm ~/.config/canvas-tec/session.json` (y, si quieres, `security delete-generic-password -s canvas-tec-session`); en Windows, `Remove-Item "$HOME\.config\canvas-tec\session.json"`. Cerrar sesión en Experiencia21 desde el navegador también la invalida.
- Si mantienes este proyecto en git, no subas `session.json` ni `browser-profile/`.

## Licencia

[MIT](LICENSE)
