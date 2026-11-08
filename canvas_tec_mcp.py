# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp[cli]>=1.2,<2",
#   "httpx>=0.27",
#   "beautifulsoup4>=4.12",
#   "pypdf>=4.0",
#   "python-pptx>=0.6.23",
#   "python-docx>=1.1",
#   "playwright==1.64.1",
#   "cryptography>=42",
# ]
# ///
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("CANVAS_BASE_URL", "https://experiencia21.tec.mx").rstrip("/")
HOST = httpx.URL(BASE_URL).host
DOWNLOADS = Path(os.environ.get("CANVAS_DOWNLOADS", "~/Downloads/Canvas")).expanduser()
MAX_TEXT = int(os.environ.get("CANVAS_MAX_TEXT", "60000"))
TZ = ZoneInfo(os.environ.get("CANVAS_TZ", "America/Mexico_City"))
CONFIG = Path(os.environ.get("CANVAS_CONFIG_DIR", "~/.config/canvas-tec")).expanduser()
PROFILE = CONFIG / "browser-profile"
SESSION_FILE = CONFIG / "session.json"
KEYCHAIN_SERVICE = "canvas-tec-session"
IDP_HOSTS = {h.strip() for h in os.environ.get("CANVAS_IDP_HOSTS", "amfs.tec.mx,aamfa.tec.mx").split(",") if h.strip()}
MAX_RPS = float(os.environ.get("CANVAS_MAX_RPS", "5"))
LOGIN_CMD = f"uv run --script {Path(__file__).resolve()} --login"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
BLOCK_TAGS = ["p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"]
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".py", ".m", ".c", ".cpp", ".h", ".ino", ".html", ".xml", ".tex"}
COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
ME_JS = """async () => {
    const r = await fetch('/api/v1/users/self', {headers: {Accept: 'application/json'}});
    return r.ok ? await r.json() : null;
}"""


def load_token() -> str:
    token = os.environ.get("CANVAS_API_TOKEN", "").strip()
    if token or sys.platform != "darwin":
        return token
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "canvas-tec", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def tail(value: str | None) -> str:
    return f"…{value[-4:]}" if value else "—"


def keychain_key(create: bool = False) -> bytes | None:
    if sys.platform != "darwin" or os.environ.get("CANVAS_SESSION_STORE") == "file":
        return None
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().encode()
        if not create:
            return None
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        subprocess.run(["security", "-i"], input=f"add-generic-password -U -s {KEYCHAIN_SERVICE} -a session-key -w {key}\n",
                       capture_output=True, text=True, timeout=10)
        return keychain_key()
    except Exception:
        return None


def session_location() -> str:
    if SESSION_FILE.exists() and not SESSION_FILE.read_bytes().lstrip().startswith(b"{"):
        return f"{SESSION_FILE} (cifrado; llave en Llavero '{KEYCHAIN_SERVICE}')"
    return str(SESSION_FILE)


def save_session(data: dict) -> str:
    CONFIG.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG, 0o700)
    raw = json.dumps(data).encode()
    key = keychain_key(create=True)
    if key:
        from cryptography.fernet import Fernet
        raw = Fernet(key).encrypt(raw)
    tmp = SESSION_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SESSION_FILE)
    return session_location()


def load_session() -> dict:
    if not SESSION_FILE.exists():
        return {}
    payload = SESSION_FILE.read_bytes()
    if payload.lstrip().startswith(b"{"):
        return json.loads(payload)
    key = keychain_key()
    if not key:
        return {}
    from cryptography.fernet import Fernet, InvalidToken
    try:
        return json.loads(Fernet(key).decrypt(payload))
    except InvalidToken:
        return {}


def keep_cookie(c: dict) -> bool:
    d = c["domain"].lstrip(".")
    return d == HOST or d in IDP_HOSTS or HOST.endswith("." + d)


def idp_cookies(data: dict) -> list[dict]:
    return [c for c in data.get("cookies", []) if c["domain"].lstrip(".") != HOST]


def browser_session(headless: bool, seed: list[dict], timeout: float) -> dict | None:
    from playwright.sync_api import sync_playwright
    CONFIG.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG, 0o700)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE), headless=headless, locale="es-MX",
                                                   viewport={"width": 1280, "height": 860})
        try:
            if seed:
                ctx.add_cookies([{k: c[k] for k in COOKIE_KEYS if k in c} for c in seed])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + timeout
            while time.time() < deadline:
                if page.is_closed():
                    return None
                url = httpx.URL(page.url)
                try:
                    if url.host == HOST and not url.path.startswith("/login"):
                        me = page.evaluate(ME_JS)
                        if me:
                            now = datetime.now(timezone.utc).isoformat()
                            return {"saved_at": now, "user": {"id": me.get("id"), "name": me.get("name")},
                                    "cookies": [{k: c[k] for k in COOKIE_KEYS} for c in ctx.cookies() if keep_cookie(c)]}
                    if headless and url.host in IDP_HOSTS and page.locator("input[type=password]").count():
                        return None
                except Exception:
                    pass
                page.wait_for_timeout(1500)
            return None
        finally:
            try:
                ctx.close()
            except Exception:
                pass


class Throttle:
    def __init__(self, rate: float):
        self.gap = 1 / rate
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            delay = self.next - now
            self.next = max(now, self.next) + self.gap
        if delay > 0:
            time.sleep(delay)


class SessionExpired(RuntimeError):
    pass


class CanvasClient(httpx.Client):
    def __init__(self, mode: str, **kwargs):
        super().__init__(**kwargs)
        self.mode = mode
        self.session: dict = {}
        self.refreshes = 0
        self.failed_refresh_at = 0.0
        self.refresh_lock = threading.Lock()

    def use_session(self, data: dict) -> None:
        self.session = data
        self.cookies.clear()
        for c in data.get("cookies", []):
            if c["domain"].lstrip(".") == HOST:
                self.cookies.set(c["name"], c["value"], domain=HOST, path=c.get("path") or "/")

    def csrf(self) -> str | None:
        try:
            value = self.cookies.get("_csrf_token", domain=HOST)
        except httpx.CookieConflict:
            value = None
        return unquote(value) if value else None

    def expired(self, r: httpx.Response) -> bool:
        url = r.request.url
        if url.host in IDP_HOSTS or (url.host == HOST and url.path.startswith("/login")):
            return True
        if r.status_code == 401 and url.host == HOST:
            r.read()
            return b"unauthenticated" in r.content
        return False

    def rate_limited(self, r: httpx.Response) -> bool:
        if r.status_code == 429:
            return True
        if r.status_code == 403:
            r.read()
            return b"Rate Limit Exceeded" in r.content
        return False

    def reload(self) -> bool:
        data = load_session()
        if data and data.get("saved_at") != self.session.get("saved_at"):
            self.use_session(data)
            return True
        return False

    def refresh(self) -> bool:
        with self.refresh_lock:
            if not idp_cookies(self.session) or time.time() - self.failed_refresh_at < 120:
                return False
            try:
                with ThreadPoolExecutor(1) as pool:
                    data = pool.submit(browser_session, True, idp_cookies(self.session), 45).result()
            except Exception:
                data = None
            if not data:
                self.failed_refresh_at = time.time()
                return False
            data["login_at"] = self.session.get("login_at") or data["saved_at"]
            save_session(data)
            self.use_session(data)
            self.refreshes += 1
            return True

    def send(self, request: httpx.Request, **kwargs) -> httpx.Response:
        own = request.url.host == HOST
        refreshed = False
        for attempt in range(6):
            THROTTLE.wait()
            if self.mode == "session" and own and request.method not in SAFE_METHODS and (token := self.csrf()):
                request.headers["X-CSRF-Token"] = token
            r = super().send(request, **kwargs)
            if self.rate_limited(r) and attempt < 5:
                retry = r.headers.get("retry-after", "")
                r.close()
                time.sleep(float(retry) if retry.replace(".", "", 1).isdigit() else min(30, 2 ** attempt))
                continue
            if self.mode == "session" and own and self.expired(r):
                r.close()
                if refreshed or not (self.reload() or self.refresh()):
                    label = "Sesión expirada" if self.session else "Sin sesión de Canvas"
                    raise SessionExpired(f"{label}: corre `{LOGIN_CMD}`")
                refreshed = True
                request.headers.pop("Cookie", None)
                self.cookies.set_cookie_header(request)
                continue
            return r
        return r


THROTTLE = Throttle(MAX_RPS)
TOKEN = "" if os.environ.get("CANVAS_AUTH") == "session" else load_token()
AUTH_MODE = "token" if TOKEN else "session"
client = CanvasClient(
    AUTH_MODE,
    base_url=f"{BASE_URL}/api/v1",
    headers={"Authorization": f"Bearer {TOKEN}"} if TOKEN else {},
    timeout=60,
    follow_redirects=True,
)
if AUTH_MODE == "session":
    client.use_session(load_session())
uploader = httpx.Client(timeout=300)
mcp = FastMCP("canvas-tec")
logging.getLogger("httpx").setLevel(logging.WARNING)


def check(r: httpx.Response) -> httpx.Response:
    if r.is_error:
        r.read()
        raise RuntimeError(f"Canvas {r.status_code} en {r.request.method} {r.request.url.path}: {r.text[:400]}")
    return r


def fetch(path: str, params: dict | None = None, limit: int = 1000):
    items, url, query = [], path, {"per_page": 100, **(params or {})}
    while url and len(items) < limit:
        r = check(client.get(url, params=query))
        data = r.json()
        if not isinstance(data, list):
            return data
        items.extend(data)
        url = r.links.get("next", {}).get("url")
        query = None
    return items[:limit]


def post(path: str, data=None, json_body=None):
    return check(client.post(path, data=data, json=json_body)).json()


def local(ts: str | None) -> str | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M (%a)")


def clip(text: str, offset: int = 0, size: int = MAX_TEXT) -> dict:
    chunk = text[offset:offset + size]
    end = offset + len(chunk)
    return {"text": chunk, "offset": offset, "total_chars": len(text), "next_offset": end if end < len(text) else None}


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for frame in soup.find_all("iframe"):
        frame.replace_with(f"\n[contenido embebido: {frame.get('src', '')}]\n")
    for img in soup.find_all("img"):
        img.replace_with(f"[imagen: {img.get('alt') or img.get('src', '')}]")
    for a in soup.find_all("a", href=True):
        a.append(f" <{a['href']}>")
    for li in soup.find_all("li"):
        li.insert(0, "• ")
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.insert_after(" | ")
    text = soup.get_text("")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def linked_file_ids(html: str | None) -> list[int]:
    return sorted({int(m) for m in re.findall(r"/files/(\d+)", html or "")})


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "archivo"


def convert_legacy(path: Path) -> Path | None:
    office = shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    if not Path(office).exists() and not shutil.which(office):
        return None
    target = {".ppt": "pptx", ".doc": "docx"}[path.suffix.lower()]
    out_dir = Path(tempfile.mkdtemp())
    subprocess.run([office, "--headless", "--convert-to", target, "--outdir", str(out_dir), str(path)],
                   capture_output=True, timeout=180)
    converted = out_dir / f"{path.stem}.{target}"
    return converted if converted.exists() else None


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".ppt", ".doc"}:
        converted = convert_legacy(path)
        if not converted:
            return f"[{suffix} antiguo: instala LibreOffice para extraer texto]"
        path, suffix = converted, converted.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n\n".join(f"--- Página {i} ---\n{(p.extract_text() or '').strip()}" for i, p in enumerate(reader.pages, 1))
    if suffix == ".pptx":
        from pptx import Presentation
        slides = []
        for i, slide in enumerate(Presentation(str(path)).slides, 1):
            parts = []
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    parts.append(shape.text_frame.text.strip())
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        parts.append(" | ".join(c.text.strip() for c in row.cells))
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                parts.append(f"[Notas] {slide.notes_slide.notes_text_frame.text.strip()}")
            slides.append(f"--- Diapositiva {i} ---\n" + "\n".join(parts))
        return "\n\n".join(slides)
    if suffix == ".docx":
        import docx
        document = docx.Document(str(path))
        parts = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            parts.extend(" | ".join(c.text.strip() for c in row.cells) for row in table.rows)
        return "\n".join(parts)
    if suffix in TEXT_SUFFIXES:
        return path.read_text(errors="replace")
    return ""


def course_dir(course_id: int | None) -> Path:
    if not course_id:
        return DOWNLOADS / "_sin_curso"
    try:
        name = fetch(f"/courses/{course_id}").get("name", str(course_id))
    except RuntimeError:
        name = str(course_id)
    return DOWNLOADS / safe_name(name)


@mcp.tool()
def whoami() -> dict:
    """Perfil del usuario dueño del token (nombre, correo, zona horaria)."""
    p = fetch("/users/self/profile")
    return {k: p.get(k) for k in ("id", "name", "short_name", "primary_email", "login_id", "time_zone")}


@mcp.tool()
def list_courses(include_past: bool = False) -> list[dict]:
    """Cursos inscritos. include_past=True agrega cursos terminados."""
    params = {"include[]": ["term", "total_scores"]}
    if not include_past:
        params["enrollment_state"] = "active"
    return [
        {
            "id": c["id"],
            "name": c.get("name"),
            "code": c.get("course_code"),
            "term": (c.get("term") or {}).get("name"),
            "score": next((e.get("computed_current_score") for e in c.get("enrollments", []) if "computed_current_score" in e), None),
        }
        for c in fetch("/courses", params)
        if c.get("name")
    ]


@mcp.tool()
def get_course(course_id: int) -> dict:
    """Información del curso: temario (syllabus) en texto y pestañas disponibles, incluidas herramientas externas."""
    c = fetch(f"/courses/{course_id}", {"include[]": ["syllabus_body", "term"]})
    tabs = fetch(f"/courses/{course_id}/tabs")
    return {
        "id": c["id"],
        "name": c.get("name"),
        "code": c.get("course_code"),
        "term": (c.get("term") or {}).get("name"),
        "syllabus": html_to_text(c.get("syllabus_body")),
        "syllabus_file_ids": linked_file_ids(c.get("syllabus_body")),
        "tabs": [{"label": t.get("label"), "type": t.get("type"), "url": t.get("full_url")} for t in tabs if not t.get("hidden")],
    }


@mcp.tool()
def list_modules(course_id: int) -> list[dict]:
    """Módulos del curso con todos sus elementos (páginas, archivos, tareas, enlaces) y estado de completado."""
    modules = fetch(f"/courses/{course_id}/modules", {"include[]": ["items", "content_details"]})
    result = []
    for m in modules:
        items = m.get("items")
        if items is None:
            items = fetch(f"/courses/{course_id}/modules/{m['id']}/items", {"include[]": "content_details"})
        result.append({
            "id": m["id"],
            "name": m.get("name"),
            "state": m.get("state"),
            "unlock_at": local(m.get("unlock_at")),
            "items": [
                {
                    "id": i["id"],
                    "title": i.get("title"),
                    "type": i.get("type"),
                    "content_id": i.get("content_id"),
                    "page_url": i.get("page_url"),
                    "external_url": i.get("external_url"),
                    "html_url": i.get("html_url"),
                    "due": local((i.get("content_details") or {}).get("due_at")),
                    "completed": (i.get("completion_requirement") or {}).get("completed"),
                }
                for i in items
            ],
        })
    return result


@mcp.tool()
def list_pages(course_id: int, search: str | None = None) -> list[dict]:
    """Páginas wiki del curso. search filtra por título."""
    params = {"sort": "title"}
    if search:
        params["search_term"] = search
    return [{"url": p["url"], "title": p.get("title"), "updated": local(p.get("updated_at"))} for p in fetch(f"/courses/{course_id}/pages", params)]


@mcp.tool()
def get_page(course_id: int, page_url: str, offset: int = 0) -> dict:
    """Contenido de una página en texto. page_url es el slug (p. ej. 'semana-3') o 'front_page'."""
    p = fetch(f"/courses/{course_id}/pages/{page_url}")
    return {
        "title": p.get("title"),
        "updated": local(p.get("updated_at")),
        "file_ids": linked_file_ids(p.get("body")),
        **clip(html_to_text(p.get("body")), offset),
    }


@mcp.tool()
def list_assignments(course_id: int, bucket: str | None = None) -> list[dict]:
    """Tareas del curso con fecha límite (hora local) y estado de tu entrega. bucket: upcoming, overdue, past, undated, ungraded, unsubmitted, future."""
    params = {"include[]": ["submission"], "order_by": "due_at"}
    if bucket:
        params["bucket"] = bucket
    out = []
    for a in fetch(f"/courses/{course_id}/assignments", params):
        s = a.get("submission") or {}
        out.append({
            "id": a["id"],
            "name": a.get("name"),
            "due": local(a.get("due_at")),
            "lock": local(a.get("lock_at")),
            "points": a.get("points_possible"),
            "types": a.get("submission_types"),
            "state": s.get("workflow_state"),
            "score": s.get("score"),
            "late": s.get("late"),
            "missing": s.get("missing"),
        })
    return out


@mcp.tool()
def get_assignment(course_id: int, assignment_id: int) -> dict:
    """Detalle de una tarea: instrucciones en texto, rúbrica, extensiones permitidas, archivos enlazados y tu entrega con comentarios."""
    a = fetch(f"/courses/{course_id}/assignments/{assignment_id}", {"include[]": ["submission"]})
    s = fetch(f"/courses/{course_id}/assignments/{assignment_id}/submissions/self",
              {"include[]": ["submission_comments", "rubric_assessment"]})
    return {
        "name": a.get("name"),
        "due": local(a.get("due_at")),
        "unlock": local(a.get("unlock_at")),
        "lock": local(a.get("lock_at")),
        "points": a.get("points_possible"),
        "types": a.get("submission_types"),
        "allowed_extensions": a.get("allowed_extensions"),
        "attempts_allowed": a.get("allowed_attempts"),
        "description": html_to_text(a.get("description")),
        "file_ids": linked_file_ids(a.get("description")),
        "rubric": [
            {"criterion": r.get("description"), "points": r.get("points"),
             "ratings": [f"{x.get('points')}: {x.get('description')}" for x in r.get("ratings", [])]}
            for r in a.get("rubric") or []
        ],
        "submission": {
            "state": s.get("workflow_state"),
            "submitted": local(s.get("submitted_at")),
            "attempt": s.get("attempt"),
            "score": s.get("score"),
            "grade": s.get("grade"),
            "files": [att.get("display_name") for att in s.get("attachments", [])],
            "comments": [f"{c.get('author_name')}: {c.get('comment')}" for c in s.get("submission_comments", [])],
            "rubric_assessment": s.get("rubric_assessment"),
        },
        "html_url": a.get("html_url"),
    }


@mcp.tool()
def list_announcements(course_id: int | None = None, days: int = 30) -> list[dict]:
    """Anuncios de los últimos `days` días. Sin course_id trae los de todos los cursos activos."""
    ids = [course_id] if course_id else [c["id"] for c in list_courses()]
    if not ids:
        return []
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    params = {"context_codes[]": [f"course_{i}" for i in ids], "start_date": start, "active_only": "true"}
    return [
        {
            "id": a["id"],
            "course_id": int(str(a.get("context_code", "_0")).split("_")[-1]),
            "title": a.get("title"),
            "author": (a.get("author") or {}).get("display_name"),
            "posted": local(a.get("posted_at")),
            "message": html_to_text(a.get("message")),
            "file_ids": linked_file_ids(a.get("message")),
        }
        for a in fetch("/announcements", params)
    ]


@mcp.tool()
def list_discussions(course_id: int) -> list[dict]:
    """Foros de discusión del curso."""
    return [
        {"id": d["id"], "title": d.get("title"), "due": local((d.get("assignment") or {}).get("due_at")),
         "replies": d.get("discussion_subentry_count"), "last_reply": local(d.get("last_reply_at"))}
        for d in fetch(f"/courses/{course_id}/discussion_topics")
    ]


@mcp.tool()
def get_discussion(course_id: int, topic_id: int) -> dict:
    """Foro completo: consigna y todas las respuestas en hilo."""
    topic = fetch(f"/courses/{course_id}/discussion_topics/{topic_id}")
    try:
        view = fetch(f"/courses/{course_id}/discussion_topics/{topic_id}/view")
    except RuntimeError as e:
        view = {"error": str(e)}
    names = {p["id"]: p.get("display_name") for p in view.get("participants", [])} if isinstance(view, dict) else {}

    def flatten(entries, depth=0):
        rows = []
        for e in entries or []:
            if not e.get("deleted"):
                rows.append({"id": e["id"], "depth": depth, "author": names.get(e.get("user_id")),
                             "date": local(e.get("created_at")), "message": html_to_text(e.get("message"))})
            rows.extend(flatten(e.get("replies"), depth + 1))
        return rows

    return {
        "title": topic.get("title"),
        "prompt": html_to_text(topic.get("message")),
        "file_ids": linked_file_ids(topic.get("message")),
        "require_initial_post": topic.get("require_initial_post"),
        "entries": flatten(view.get("view")) if isinstance(view, dict) else [],
        "note": view.get("error") if isinstance(view, dict) else None,
    }


@mcp.tool()
def list_files(course_id: int, search: str | None = None) -> list[dict]:
    """Archivos del curso (pestaña Archivos). Si el profesor la ocultó, usa list_modules y los file_ids de las páginas."""
    params = {"sort": "updated_at", "order": "desc"}
    if search:
        params["search_term"] = search
    try:
        files = fetch(f"/courses/{course_id}/files", params)
    except RuntimeError as e:
        return [{"error": str(e), "hint": "Archivos oculto: busca los archivos en list_modules o get_page"}]
    return [
        {"id": f["id"], "name": f.get("display_name"), "type": f.get("content-type"),
         "size_kb": round((f.get("size") or 0) / 1024), "updated": local(f.get("updated_at")), "locked": f.get("locked_for_user")}
        for f in files
    ]


@mcp.tool()
def download_file(file_id: int, extract: bool = True, offset: int = 0) -> dict:
    """Descarga un archivo de Canvas a ~/Downloads/Canvas/<curso>/ y devuelve su texto (PDF, PPTX, DOCX, PPT/DOC con LibreOffice, código). Usa offset para leer textos largos por partes."""
    meta = fetch(f"/files/{file_id}")
    if meta.get("locked_for_user"):
        raise RuntimeError(f"Archivo bloqueado: {meta.get('lock_explanation', '')}")
    course_id = None
    m = re.search(r"/courses/(\d+)/", meta.get("url", "") + " " + str(meta.get("preview_url", "")))
    if m:
        course_id = int(m.group(1))
    elif meta.get("folder_id"):
        try:
            folder = fetch(f"/folders/{meta['folder_id']}")
            if folder.get("context_type") == "Course":
                course_id = folder.get("context_id")
        except RuntimeError:
            pass
    target_dir = course_dir(course_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / safe_name(meta.get("display_name") or meta.get("filename") or str(file_id))
    if not (target.exists() and target.stat().st_size == meta.get("size")):
        with client.stream("GET", meta["url"]) as r:
            check(r)
            with target.open("wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)
    result = {"path": str(target), "name": target.name, "type": meta.get("content-type"), "size_kb": round(target.stat().st_size / 1024)}
    if extract:
        text = extract_text(target)
        if not text:
            result.update({"text": None, "note": "Formato sin extracción de texto; abre el archivo en path"})
        else:
            result.update(clip(text, offset))
            if len(re.sub(r"--- (Página|Diapositiva) \d+ ---|\s", "", text)) < 20:
                result["note"] = "Casi sin texto (escaneo o imágenes): abre el archivo en path para verlo"
    return result


@mcp.tool()
def read_local_file(path: str, offset: int = 0) -> dict:
    """Extrae texto de un archivo local ya descargado (PDF, PPTX, DOCX, texto)."""
    p = Path(path).expanduser()
    if not p.exists():
        raise RuntimeError(f"No existe: {p}")
    return {"path": str(p), **clip(extract_text(p), offset)}


@mcp.tool()
def get_grades(course_id: int | None = None) -> dict | list:
    """Sin course_id: calificación actual de cada curso. Con course_id: calificación por tarea."""
    if course_id is None:
        enrollments = fetch("/users/self/enrollments", {"type[]": "StudentEnrollment", "state[]": "active"})
        names = {c["id"]: c["name"] for c in list_courses()}
        return [
            {"course_id": e["course_id"], "course": names.get(e["course_id"]),
             "current_score": (e.get("grades") or {}).get("current_score"),
             "final_score": (e.get("grades") or {}).get("final_score"),
             "current_grade": (e.get("grades") or {}).get("current_grade")}
            for e in enrollments
        ]
    subs = fetch(f"/courses/{course_id}/students/submissions", {"student_ids[]": "self", "include[]": ["assignment"]})
    return {
        "course_id": course_id,
        "items": [
            {"assignment": (s.get("assignment") or {}).get("name"),
             "points_possible": (s.get("assignment") or {}).get("points_possible"),
             "score": s.get("score"), "grade": s.get("grade"), "state": s.get("workflow_state"),
             "late": s.get("late"), "missing": s.get("missing"), "graded": local(s.get("graded_at"))}
            for s in subs
        ],
    }


@mcp.tool()
def get_todo(days_ahead: int = 14) -> list[dict]:
    """Pendientes de todos los cursos (agenda de Canvas): tareas, quizzes, foros y eventos de hoy a `days_ahead` días."""
    now = datetime.now(timezone.utc)
    params = {"start_date": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end_date": (now + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    items = fetch("/planner/items", params)
    return [
        {"type": i.get("plannable_type"), "title": (i.get("plannable") or {}).get("title"),
         "course": i.get("context_name"), "course_id": i.get("course_id"),
         "due": local(i.get("plannable_date")),
         "submitted": (i.get("submissions") or {}).get("submitted") if isinstance(i.get("submissions"), dict) else None,
         "html_url": i.get("html_url")}
        for i in items
    ]


@mcp.tool()
def list_quizzes(course_id: int) -> list[dict]:
    """Exámenes clásicos del curso (los New Quizzes aparecen como tareas)."""
    return [
        {"id": q["id"], "title": q.get("title"), "due": local(q.get("due_at")), "unlock": local(q.get("unlock_at")),
         "lock": local(q.get("lock_at")), "time_limit_min": q.get("time_limit"), "attempts": q.get("allowed_attempts"),
         "points": q.get("points_possible"), "description": html_to_text(q.get("description"))}
        for q in fetch(f"/courses/{course_id}/quizzes")
    ]


@mcp.tool()
def open_url(url: str) -> dict:
    """Abre cualquier enlace de Canvas que pegue el usuario (página, archivo, tarea, foro, elemento de módulo) y devuelve su contenido."""
    path = url.split(BASE_URL)[-1] if BASE_URL in url else url
    if m := re.search(r"/files/(\d+)", path):
        return download_file(int(m.group(1)))
    if m := re.search(r"/courses/(\d+)/pages/([^/?#]+)", path):
        return get_page(int(m.group(1)), m.group(2))
    if m := re.search(r"/courses/(\d+)/assignments/(\d+)", path):
        return get_assignment(int(m.group(1)), int(m.group(2)))
    if m := re.search(r"/courses/(\d+)/discussion_topics/(\d+)", path):
        return get_discussion(int(m.group(1)), int(m.group(2)))
    if m := re.search(r"/courses/(\d+)/quizzes/(\d+)", path):
        q = fetch(f"/courses/{m.group(1)}/quizzes/{m.group(2)}")
        return {"title": q.get("title"), "description": html_to_text(q.get("description")), "due": local(q.get("due_at"))}
    if m := re.search(r"/courses/(\d+)/modules/items/(\d+)", path):
        seq = fetch(f"/courses/{m.group(1)}/module_item_sequence", {"asset_type": "ModuleItem", "asset_id": m.group(2)})
        item = ((seq.get("items") or [{}])[0].get("current")) or {}
        kind, cid = item.get("type"), int(m.group(1))
        if kind == "Page":
            return get_page(cid, item["page_url"])
        if kind == "File":
            return download_file(item["content_id"])
        if kind == "Assignment":
            return get_assignment(cid, item["content_id"])
        if kind == "Discussion":
            return get_discussion(cid, item["content_id"])
        return {"type": kind, "title": item.get("title"), "external_url": item.get("external_url"), "html_url": item.get("html_url")}
    if m := re.search(r"/courses/(\d+)/?$", path.split("?")[0]):
        return get_course(int(m.group(1)))
    raise RuntimeError("Enlace no reconocido; usa canvas_get con la ruta de la API")


@mcp.tool()
def canvas_get(path: str, params: str = "{}") -> dict | list:
    """Lectura genérica (solo GET) de cualquier endpoint de la API de Canvas, p. ej. '/courses/123/rubrics' o '/users/self/groups'. params es JSON."""
    if not path.startswith("/"):
        path = "/" + path
    path = re.sub(r"^/api/v1", "", path)
    data = fetch(path, json.loads(params or "{}"), limit=300)
    text = json.dumps(data, ensure_ascii=False)
    return data if len(text) <= MAX_TEXT else clip(text)


@mcp.tool()
def submit_assignment(
    course_id: int,
    assignment_id: int,
    file_paths: list[str] | None = None,
    text: str | None = None,
    url: str | None = None,
    comment: str | None = None,
) -> dict:
    """ENTREGA una tarea (irreversible: cuenta como intento). Antes de llamarla, confirma con el usuario curso, tarea y archivos. Usa file_paths (rutas locales), text (HTML/texto) o url, según los tipos que acepte la tarea."""
    if file_paths:
        file_ids = []
        for raw in file_paths:
            f = Path(raw).expanduser()
            if not f.exists():
                raise RuntimeError(f"No existe: {f}")
            slot = post(f"/courses/{course_id}/assignments/{assignment_id}/submissions/self/files",
                        data={"name": f.name, "size": f.stat().st_size})
            with f.open("rb") as fh:
                up = uploader.post(slot["upload_url"], data=slot.get("upload_params", {}), files={"file": (f.name, fh)})
            if up.is_redirect:
                up = client.get(up.headers["location"])
            check(up)
            file_ids.append(up.json()["id"])
        data = {"submission[submission_type]": "online_upload", "submission[file_ids][]": file_ids}
    elif text:
        data = {"submission[submission_type]": "online_text_entry", "submission[body]": text}
    elif url:
        data = {"submission[submission_type]": "online_url", "submission[url]": url}
    else:
        raise RuntimeError("Indica file_paths, text o url")
    if comment:
        data["comment[text_comment]"] = comment
    s = post(f"/courses/{course_id}/assignments/{assignment_id}/submissions", data=data)
    return {"submission_id": s.get("id"), "state": s.get("workflow_state"), "attempt": s.get("attempt"),
            "submitted": local(s.get("submitted_at")), "late": s.get("late")}


@mcp.tool()
def post_discussion_reply(course_id: int, topic_id: int, message: str, parent_entry_id: int | None = None) -> dict:
    """PUBLICA en un foro (visible para el grupo). Confirma el texto con el usuario antes de llamarla. parent_entry_id responde a una entrada concreta."""
    path = f"/courses/{course_id}/discussion_topics/{topic_id}/entries"
    if parent_entry_id:
        path += f"/{parent_entry_id}/replies"
    e = post(path, data={"message": message})
    return {"entry_id": e.get("id"), "created": local(e.get("created_at"))}


@mcp.tool()
def session_status() -> dict:
    """Estado de la autenticación con Canvas: si la sesión web sigue viva, a nombre de quién, cuándo se inició y si se renovó sola."""
    if AUTH_MODE == "token":
        try:
            me = whoami()
            return {"mode": "token", "alive": True, "user": me.get("name"), "email": me.get("primary_email")}
        except RuntimeError as e:
            return {"mode": "token", "alive": False, "error": str(e)}
    before = client.refreshes
    info = {
        "mode": "session",
        "store": session_location(),
        "login_at": local(client.session.get("login_at")),
        "refreshed_at": local(client.session.get("saved_at")),
        "silent_refresh_available": bool(idp_cookies(client.session)),
    }
    try:
        me = whoami()
    except RuntimeError as e:
        return {**info, "alive": False, "error": str(e), "login_command": LOGIN_CMD}
    return {**info, "alive": True, "user": me.get("name"), "email": me.get("primary_email"),
            "login_id": me.get("login_id"), "renewed_now": client.refreshes > before,
            "refreshed_at": local(client.session.get("saved_at"))}


def palette() -> tuple[str, ...]:
    return "\033[32m", "\033[31m", "\033[36m", "\033[2m", "\033[1m", "\033[33m", "\033[0m"


def login_cli() -> int:
    green, red, cyan, dim, bold, yellow, reset = palette()
    print(f"{bold}{cyan}canvas-tec login{reset} {dim}{BASE_URL}{reset}")
    previous = load_session()
    print(f"{dim}Abriendo Chromium ({PROFILE}). Inicia sesión con SSO + MFA; tienes 10 minutos.{reset}")
    if idp_cookies(previous):
        print(f"{dim}Si la sesión del IdP sigue viva, entrará sola.{reset}")
    try:
        data = browser_session(False, idp_cookies(previous), 600)
    except Exception as e:
        print(f"{red}✗ No se pudo abrir Chromium:{reset} {str(e).splitlines()[0]}")
        print(f"{dim}Instálalo con: uv run --with playwright==1.64.1 playwright install chromium{reset}")
        return 1
    if not data:
        print(f"{red}✗ No se detectó el inicio de sesión{reset} {dim}(ventana cerrada o tiempo agotado){reset}")
        return 1
    data["login_at"] = data["saved_at"]
    where = save_session(data)
    print(f"{green}✓ Sesión iniciada{reset} → {bold}{data['user']['name']}{reset}")
    for c in sorted(data["cookies"], key=lambda c: (c["domain"].lstrip(".") != HOST, c["domain"], c["name"])):
        print(f"  {cyan}{c['name'][:34]:<34}{reset} {dim}{c['domain']:<22}{reset} {tail(c['value'])}")
    print(f"{green}✓ Guardada{reset} {dim}{where}{reset}")
    return 0


def self_test() -> int:
    green, red, cyan, dim, bold, yellow, reset = palette()
    print(f"{bold}{cyan}canvas-tec{reset} {dim}{BASE_URL} · modo {AUTH_MODE}{reset}")
    if AUTH_MODE == "session" and not client.session:
        print(f"{red}✗ Sin sesión{reset} {dim}corre: {LOGIN_CMD}{reset}")
        return 1
    try:
        me = whoami()
        if AUTH_MODE == "token":
            print(f"{green}✓ Token válido{reset} → {bold}{me['name']}{reset} {dim}{me.get('primary_email') or ''}{reset}")
        else:
            if client.refreshes:
                print(f"{yellow}↻ Sesión renovada en silencio con el IdP{reset}")
            print(f"{green}✓ Sesión válida{reset} → {bold}{me['name']}{reset} {dim}{me.get('primary_email') or ''}{reset}")
            print(f"  {dim}canvas_session {tail(client.cookies.get('canvas_session', domain=HOST))} · login {local(client.session.get('login_at'))} · {session_location()}{reset}")
        courses = list_courses()
        print(f"{green}✓ {len(courses)} cursos activos{reset}")
        for c in courses:
            score = f"{c['score']:.1f}" if isinstance(c.get("score"), (int, float)) else "—"
            print(f"  {cyan}{c['id']:>8}{reset}  {c['name']}  {dim}{score}{reset}")
        todo = get_todo(7)
        print(f"{green}✓ {len(todo)} pendientes en 7 días{reset}")
        for t in todo[:10]:
            print(f"  {dim}{t['due']}{reset}  {t['title']}  {dim}{t['course'] or ''}{reset}")
        return 0
    except Exception as e:
        print(f"{red}✗ {e}{reset}")
        return 1


if __name__ == "__main__":
    if "--login" in sys.argv:
        sys.exit(login_cli())
    if "--test" in sys.argv:
        sys.exit(self_test())
    mcp.run()
