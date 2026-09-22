"""Docs técnicas privadas (`/docs/`) -- documentación de referencia de la API
y el changelog, ambas staff-only (mismo login/sesión que ya usa `/admin/`,
no un mecanismo nuevo).

El contenido vive en Markdown plano en el repo (`docs/reference/*.md` y
`CHANGELOG.md` en la raíz), no en la base de datos: se actualiza con un
commit normal, no hay UI de edición. `docs_index` arma el menú leyendo el
directorio, así que un `.md` nuevo aparece solo sin tocar código.
"""
import re
from pathlib import Path

import markdown as markdown_lib
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404
from django.shortcuts import render

REFERENCE_DIR = Path(settings.BASE_DIR) / "docs" / "reference"
CHANGELOG_PATH = Path(settings.BASE_DIR) / "CHANGELOG.md"

_MD_EXTENSIONS = ["extra", "fenced_code", "tables", "toc", "sane_lists"]

# Sólo letras/números/guiones -- coincide con el nombre de archivo sin
# extensión (ver `_slug_from_path`), nunca se interpola en una ruta de
# filesystem sin pasar antes por este chequeo.
_SLUG_RE = re.compile(r"^[a-z0-9-]+$")


def _slug_from_path(path: Path) -> str:
    return path.stem


def _render_markdown(title: str, text: str, *, active_slug: str | None):
    html = markdown_lib.markdown(text, extensions=_MD_EXTENSIONS)
    pages = sorted(
        (
            {"slug": _slug_from_path(p), "title": _title_from_file(p)}
            for p in REFERENCE_DIR.glob("*.md")
            if p.stem != "index"
        ),
        key=lambda p: p["title"],
    ) if REFERENCE_DIR.is_dir() else []
    return {"page_title": title, "content_html": html, "pages": pages, "active_slug": active_slug}


def _title_from_file(path: Path) -> str:
    """El título es la primera línea `# Algo` del archivo; si no la tiene,
    el nombre del archivo tal cual."""
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("# "):
                    return line[2:].strip()
                if line:
                    break
    except OSError:
        pass
    return path.stem.replace("-", " ").replace("_", " ").title()


@staff_member_required
def docs_index(request):
    readme = REFERENCE_DIR / "index.md"
    text = readme.read_text(encoding="utf-8") if readme.exists() else (
        "# Documentación\n\nElegí una sección del menú de la izquierda."
    )
    ctx = _render_markdown("Documentación", text, active_slug="index")
    return render(request, "docs/page.html", ctx)


@staff_member_required
def docs_page(request, slug: str):
    if not _SLUG_RE.match(slug):
        raise Http404
    path = REFERENCE_DIR / f"{slug}.md"
    if not path.is_file():
        raise Http404
    text = path.read_text(encoding="utf-8")
    ctx = _render_markdown(_title_from_file(path), text, active_slug=slug)
    return render(request, "docs/page.html", ctx)


@staff_member_required
def docs_changelog(request):
    if not CHANGELOG_PATH.is_file():
        raise Http404
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    ctx = _render_markdown("Changelog", text, active_slug="changelog")
    return render(request, "docs/page.html", ctx)
