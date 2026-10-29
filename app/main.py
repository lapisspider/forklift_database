"""FastAPI application: UI + endpoints for search, web lookup, save, NL query."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import auth, fitment, fitment_derive, kit_view, serial_intake, serials
from .ai import lookup as lookup_service
from .ai import nl_query
from .auth import current_user
from .config import settings
from .database import get_db, init_db
from .models import Forklift, Kit, KitFitment, KitForklift, SerialRecord, User
from .schemas import ForkliftSpecs

BASE_DIR = Path(__file__).resolve().parent
PDF_DIR = BASE_DIR.parent / "data" / "pdfs"

app = FastAPI(title="Forklift Spec Database")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _asset_version() -> str:
    """Cache-busting token from the newest static file's mtime."""
    try:
        static = BASE_DIR / "static"
        return str(int(max(p.stat().st_mtime for p in static.glob("*"))))
    except (ValueError, OSError):
        return "1"


templates.env.globals["asset_v"] = _asset_version()
app.include_router(auth.router)


# POST endpoints that a viewer may use (read-only AI actions).
_READONLY_POSTS = {"/ask", "/lookup"}


def _needs_admin(method: str, path: str) -> bool:
    """Admin-only: user management, forklift review status, connection tiers."""
    if path.startswith("/users"):
        return True
    if path.endswith("/status"):             # forklift info status (green/yellow/red)
        return True
    if path.startswith("/connection"):       # kit-connection tier promotion
        return True
    return False


def _needs_editor(method: str, path: str) -> bool:
    """Editor or admin: add / edit / delete forklifts, kits, and links."""
    if _needs_admin(method, path):
        return False                         # handled by the admin gate instead
    if path.endswith("/edit"):               # GET edit form + POST save
        return True
    if method == "POST" and path not in _READONLY_POSTS and (
        path.startswith("/forklift") or path.startswith("/kit")
        or path.startswith("/record")         # recording / superseding a serial
    ):                                        # create / delete / archive-pdf
        return True
    return False


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """Require login for non-public paths; require admin for edits/management."""
    path = request.url.path
    if any(path.startswith(p) for p in auth.PUBLIC_PREFIXES):
        return await call_next(request)
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    role = user.get("role")
    if _needs_admin(request.method, path) and role != "admin":
        return JSONResponse({"detail": "Admins only."}, status_code=403)
    if _needs_editor(request.method, path) and role not in ("admin", "editor"):
        return JSONResponse({"detail": "Editors or admins only — you have view-only access."},
                            status_code=403)
    return await call_next(request)


# SessionMiddleware is added LAST so it is the OUTERMOST layer — it must run
# before auth_guard so request.session is available inside the guard.
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                   same_site="lax", https_only=settings.session_https_only)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.on_event("startup")
def _startup() -> None:
    init_db()
    PDF_DIR.mkdir(parents=True, exist_ok=True)


def _ctx(request: Request, **kw) -> dict:
    user = current_user(request)
    role = user.get("role") if user else None
    base = {
        "request": request,
        "ai_enabled": settings.ai_enabled,
        "web_lookup_enabled": settings.web_lookup_enabled,
        "user": user,
        "is_admin": role == "admin",
        "can_edit": role in ("admin", "editor"),
    }
    base.update(kw)
    return base


# ----------------------------------------------------------------------------
# Home + browse/search
# ----------------------------------------------------------------------------
SORTS = {
    "oem": (Forklift.manufacturer, Forklift.series, Forklift.model),
    "az": (Forklift.model.asc(),),
    "za": (Forklift.model.desc(),),
    "serial": (Forklift.internal_serial.asc(),),
    "year": (Forklift.year_start.is_(None), Forklift.year_start.asc(), Forklift.model.asc()),
}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", sort: str = "oem",
          kitsort: str = "oem", db: Session = Depends(get_db)):
    if sort not in SORTS:
        sort = "oem"
    if kitsort not in kit_view.SORTS:
        kitsort = "oem"
    query = db.query(Forklift).order_by(*SORTS[sort])
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            or_(
                Forklift.manufacturer.ilike(like),
                Forklift.series.ilike(like),
                Forklift.model.ilike(like),
                Forklift.fuel_type.ilike(like),
                Forklift.internal_serial.ilike(like),
            )
        )
    forklifts = query.limit(5000).all()

    # For the OEM view, group into OEM -> series -> [forklifts] (ordered).
    oem_groups: dict[str, dict[str, list]] = {}
    if sort == "oem":
        for f in forklifts:
            series = f.series or "(no series)"
            oem_groups.setdefault(f.manufacturer, {}).setdefault(series, []).append(f)

    # The Kits tab is the kit-centric view: grouped by the TRUCK's OEM, because
    # a salesperson arrives knowing the brand of the machine in front of them.
    # It lives here rather than on a page of its own so there is one kit list.
    kit_rows = kit_view.kit_summaries(db, kitsort)
    kit_groups = kit_view.grouped_by_oem(kit_rows) if kitsort == "oem" else {}
    unlinked = (db.query(SerialRecord)
                .filter(SerialRecord.status == "active",
                        SerialRecord.kit_id.is_(None))
                .count())
    # Full forklift list for the "add kit" picker (unaffected by the filter).
    all_forklifts = db.query(Forklift).order_by(Forklift.manufacturer, Forklift.model).all()
    return templates.TemplateResponse(
        request, "index.html",
        _ctx(request, forklifts=forklifts, oem_groups=oem_groups,
             kit_rows=kit_rows, kit_groups=kit_groups, kitsort=kitsort,
             kitsorts=kit_view.SORTS, unlinked=unlinked,
             needs_attention=sum(1 for r in kit_rows if r.attention),
             all_forklifts=all_forklifts, q=q, sort=sort),
    )


@app.get("/forklift/{fid}", response_class=HTMLResponse)
def detail(fid: int, request: Request, db: Session = Depends(get_db)):
    fk = db.get(Forklift, fid)
    if not fk:
        raise HTTPException(404, "Forklift not found")
    twins = fk.twins(db)
    return templates.TemplateResponse(request, "detail.html", _ctx(request, fk=fk, twins=twins))


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse(request, "about.html", _ctx(request))


# ----------------------------------------------------------------------------
# Web lookup -> review -> confirm-save
# ----------------------------------------------------------------------------
@app.post("/lookup", response_class=HTMLResponse)
def lookup(request: Request, query: str = Form(...), web: bool = Form(False),
           mode: str = Form(""), db: Session = Depends(get_db)):
    result = lookup_service.lookup(db, query, web=web, mode=mode)
    return templates.TemplateResponse(
        request, "partials/lookup_result.html", _ctx(request, result=result, query=query)
    )


def next_company_serial(db: Session) -> str:
    """Next FLT-xxxx serial: one past the highest currently in the database.
    Serials freed by deletes below the high-water mark are never reused, so
    old paperwork/kit links can't silently attach to a different machine."""
    import re

    top = 0
    for (s,) in db.query(Forklift.internal_serial).all():
        m = re.fullmatch(r"FLT-(\d+)", s or "")
        if m:
            top = max(top, int(m.group(1)))
    return f"FLT-{top + 1:04d}"


def _num(data: dict, key: str) -> float | None:
    """Parse an optional numeric form field; bad input becomes None."""
    val = data.get(key)
    try:
        return float(val) if val not in (None, "") else None
    except ValueError:
        return None


@app.post("/forklift", response_class=HTMLResponse)
async def create(request: Request, db: Session = Depends(get_db)):
    """Save a forklift after the user confirms the reviewed specs."""
    form = await request.form()
    data = {k: (v or None) for k, v in form.items()}

    def num(key):
        return _num(data, key)

    ys, ye = _num(data, "year_start"), _num(data, "year_end")
    fk = Forklift(
        manufacturer=data.get("manufacturer") or "Unknown",
        series=data.get("series"),
        model=data.get("model") or "Unknown",
        year_start=int(ys) if ys else None,
        year_end=int(ye) if ye else None,
        internal_serial=data.get("internal_serial") or None,
        capacity_kg=num("capacity_kg"),
        fuel_type=data.get("fuel_type"),
        chassis=data.get("chassis"),
        truck_class=data.get("truck_class"),
        source_url=data.get("source_url"),
        pdf_url=data.get("pdf_url"),
        notes=data.get("notes"),
        fetched_at=datetime.now(timezone.utc),
    )
    db.add(fk)
    db.flush()
    if not fk.internal_serial:
        fk.internal_serial = next_company_serial(db)
    db.commit()
    db.refresh(fk)
    return RedirectResponse(url=f"/forklift/{fk.id}", status_code=303)


@app.get("/forklift/{fid}/edit", response_class=HTMLResponse)
def edit_forklift_form(fid: int, request: Request, db: Session = Depends(get_db)):
    fk = db.get(Forklift, fid)
    if not fk:
        raise HTTPException(404, "Forklift not found")
    return templates.TemplateResponse(request, "edit_forklift.html", _ctx(request, fk=fk))


@app.post("/forklift/{fid}/edit", response_class=HTMLResponse)
async def edit_forklift(fid: int, request: Request, db: Session = Depends(get_db)):
    fk = db.get(Forklift, fid)
    if not fk:
        raise HTTPException(404, "Forklift not found")
    form = await request.form()
    data = {k: (v.strip() if isinstance(v, str) else v) or None for k, v in form.items()}

    # Company serial: blank keeps the current one; changes must stay unique.
    old_serial = fk.internal_serial
    new_serial = data.get("internal_serial") or old_serial
    if new_serial != old_serial:
        clash = (
            db.query(Forklift)
            .filter(Forklift.internal_serial == new_serial, Forklift.id != fid)
            .first()
        )
        if clash:
            return templates.TemplateResponse(
                request, "edit_forklift.html",
                _ctx(request, fk=fk,
                     error=f'Serial "{new_serial}" is already used by '
                           f"{clash.manufacturer} {clash.model}."),
            )

    fk.manufacturer = data.get("manufacturer") or fk.manufacturer
    fk.series = data.get("series")
    fk.model = data.get("model") or fk.model
    ys, ye = _num(data, "year_start"), _num(data, "year_end")
    fk.year_start = int(ys) if ys else None
    fk.year_end = int(ye) if ye else None
    fk.capacity_kg = _num(data, "capacity_kg")
    fk.fuel_type = data.get("fuel_type")
    fk.chassis = data.get("chassis")
    fk.truck_class = data.get("truck_class")
    fk.source_url = data.get("source_url")
    fk.pdf_url = data.get("pdf_url")
    fk.notes = data.get("notes")

    if new_serial != old_serial:
        fk.internal_serial = new_serial
        # Kit links are by forklift id (many-to-many), so they survive a
        # serial change automatically — nothing to update here.

    # An editor's edit invalidates a prior admin review -> back to Yellow.
    # An admin's own edit leaves the status as they set it.
    if (current_user(request) or {}).get("role") == "editor":
        fk.info_status = "yellow"
    db.commit()
    return RedirectResponse(url=f"/forklift/{fid}", status_code=303)


@app.post("/forklift/{fid}/delete")
def delete(fid: int, db: Session = Depends(get_db)):
    fk = db.get(Forklift, fid)
    if fk:
        db.delete(fk)
        db.commit()
    return RedirectResponse(url="/", status_code=303)


# ----------------------------------------------------------------------------
# Kits: products the company sells, linked many-to-many to forklift models
# ----------------------------------------------------------------------------
def _forklifts_from_ids(db: Session, ids: list[str]) -> list[Forklift]:
    """Resolve submitted forklift-id strings to Forklift rows (order preserved)."""
    out = []
    for raw in ids:
        raw = (raw or "").strip()
        if raw.isdigit():
            fk = db.get(Forklift, int(raw))
            if fk and fk not in out:
                out.append(fk)
    return out


def _sync_kit_links(db: Session, kit: Kit, ids: list[str], reviewer: str | None) -> None:
    """Set a kit's forklift connections from submitted ids, PRESERVING the tier of
    connections that remain (only new links are added as bronze; deselected are
    removed). Avoids resetting review tiers on an unrelated kit edit."""
    want = {f.id for f in _forklifts_from_ids(db, ids)}
    current = {link.forklift_id: link for link in kit.forklift_links}
    for fid, link in list(current.items()):
        if fid not in want:
            kit.forklift_links.remove(link)          # deselected -> drop
    for fid in want:
        if fid not in current:
            fk = db.get(Forklift, fid)
            kit.forklift_links.append(
                KitForklift(forklift=fk, tier="bronze", reviewed_by=reviewer))


def _parse_price(price: str) -> float | None:
    try:
        return float(price) if price.strip() else None
    except ValueError:
        return None


@app.post("/kit")
async def create_kit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    sku = (form.get("sku") or "").strip()
    if not sku:
        raise HTTPException(400, "SKU (part number) is required.")
    kit = Kit(
        sku=sku,
        name=(form.get("name") or "").strip() or None,
        price=_parse_price(form.get("price") or ""),
        notes=(form.get("notes") or "").strip() or None,
    )
    db.add(kit)
    reviewer = (current_user(request) or {}).get("email")
    _sync_kit_links(db, kit, form.getlist("forklift_ids"), reviewer)
    db.commit()
    return RedirectResponse(url="/#kits", status_code=303)


# ----------------------------------------------------------------------------
# Kit-centric views. The kit is the spine now: these answer "which kit does
# this truck need", where the older routes answer "what do we know about this
# truck". See CLAUDE.md, "Fitment keyed on serials".
# ----------------------------------------------------------------------------
@app.get("/kits")
def kit_list_redirect():
    """The kit list lives in the Kits tab on the home page -- there is one kit
    list, not two. Kept as a redirect so older links and bookmarks still land."""
    return RedirectResponse(url="/#kits", status_code=307)


@app.get("/kit/{kid}", response_class=HTMLResponse)
def kit_detail(kid: int, request: Request, db: Session = Depends(get_db)):
    kit = db.get(Kit, kid)
    if not kit:
        raise HTTPException(404, "Kit not found")
    blocks = kit_view.kit_models(db, kit)
    return templates.TemplateResponse(
        request, "kit_detail.html",
        _ctx(request, kit=kit, blocks=blocks,
             superseded=[r for r in kit.serial_records if r.status != "active"]),
    )


@app.get("/fit", response_class=HTMLResponse)
def fit_page(request: Request, oem: str = "", model: str = "", serial: str = "",
             db: Session = Depends(get_db)):
    """The salesperson's lookup. A GET so a result can be sent to someone."""
    oems = [row[0] for row in db.query(KitFitment.manufacturer)
            .distinct().order_by(KitFitment.manufacturer).all()]
    result = None
    if oem and model:
        result = _fit_result(db, oem, model, serial)
    return templates.TemplateResponse(
        request, "fit.html",
        _ctx(request, oems=oems, oem=oem, model=model, serial=serial, result=result),
    )


@app.post("/fit", response_class=HTMLResponse)
def fit_lookup(request: Request, oem: str = Form(""), model: str = Form(""),
               serial: str = Form(""), db: Session = Depends(get_db)):
    """The same lookup over HTMX, for the result panel alone."""
    return templates.TemplateResponse(
        request, "partials/fit_result.html",
        _ctx(request, result=_fit_result(db, oem, model, serial)),
    )


def _fit_result(db: Session, oem: str, model: str, serial: str) -> dict:
    """Everything the result panel needs, including why it is empty."""
    oem, model, serial = oem.strip(), model.strip(), serial.strip()
    if not oem or not model:
        return {"error": "Pick a truck manufacturer and type a model."}

    hits = fitment.kits_for(db, oem, model, serial or None)
    note = None
    if serial and not serials.scheme_is_ordered(oem):
        # Say this before the results, not after: it changes how every verdict
        # below should be read.
        note = serials.explain(oem)
    return {
        "oem": oem, "model": model, "serial": serial,
        "hits": hits,
        "quotable": fitment.confirmed(hits),
        "unconfirmed": [h for h in hits if h.verdict == fitment.UNCONFIRMED],
        "excluded": [h for h in hits if h.verdict == fitment.EXCLUDED],
        "scheme_note": note,
        "records": fitment.records_for(db, oem, model, serial or None),
    }


@app.get("/records", response_class=HTMLResponse)
def record_list(request: Request, db: Session = Depends(get_db)):
    """Order records, newest first, with the form for typing one in.

    The CSV importer is the bulk path; this is the one-off a clerk types when a
    single kit ships. Both go through app/serial_intake.py so they cannot
    disagree about what a valid record is.
    """
    records = (db.query(SerialRecord)
               .order_by(SerialRecord.recorded_at.desc(), SerialRecord.id.desc())
               .limit(500).all())
    oems = [row[0] for row in db.query(KitFitment.manufacturer)
            .distinct().order_by(KitFitment.manufacturer).all()]
    return templates.TemplateResponse(
        request, "records.html",
        _ctx(request, records=records, oems=oems,
             total=db.query(SerialRecord).count()),
    )


@app.post("/record")
def create_record(request: Request, part_number: str = Form(""),
                  manufacturer: str = Form(""), model: str = Form(""),
                  serial: str = Form(""), customer: str = Form(""),
                  location: str = Form(""), order_ref: str = Form(""),
                  note: str = Form(""), db: Session = Depends(get_db)):
    row = serial_intake.judge(db, 0, {
        "part_number": part_number, "manufacturer": manufacturer,
        "model": model, "serial": serial, "customer": customer,
        "location": location, "order_ref": order_ref, "note": note,
    })
    if row.errors:
        raise HTTPException(400, "; ".join(row.errors))
    if row.duplicate_of is not None:
        # Already on record. Not an error -- the clerk is told, not scolded.
        return RedirectResponse(url="/records?dup=1", status_code=303)

    recorded_by = (current_user(request) or {}).get("email")
    db.add(serial_intake.to_record(row, recorded_by))
    db.commit()
    # The record changes what the derived ranges say, so rebuild them now
    # rather than leaving the kit pages stale until someone runs the script.
    fitment_derive.apply(db)
    return RedirectResponse(url="/records?saved=1", status_code=303)


@app.post("/record/{rid}/supersede")
def supersede_record(rid: int, request: Request, db: Session = Depends(get_db)):
    """Retire a record without deleting it.

    The derived ranges are rebuilt FROM these rows, so deleting one would
    rewrite history with nothing left to show what changed. A superseded row
    keeps its data and stops counting.
    """
    rec = db.get(SerialRecord, rid)
    if not rec:
        raise HTTPException(404, "Record not found")
    rec.status = "superseded"
    db.commit()
    fitment_derive.apply(db)
    return RedirectResponse(url="/records", status_code=303)


@app.get("/kit/{kid}/edit", response_class=HTMLResponse)
def edit_kit_form(kid: int, request: Request, db: Session = Depends(get_db)):
    kit = db.get(Kit, kid)
    if not kit:
        raise HTTPException(404, "Kit not found")
    all_forklifts = db.query(Forklift).order_by(Forklift.manufacturer, Forklift.model).all()
    return templates.TemplateResponse(
        request, "edit_kit.html", _ctx(request, kit=kit, all_forklifts=all_forklifts)
    )


@app.post("/kit/{kid}/edit")
async def edit_kit(kid: int, request: Request, db: Session = Depends(get_db)):
    kit = db.get(Kit, kid)
    if not kit:
        raise HTTPException(404, "Kit not found")
    form = await request.form()
    sku = (form.get("sku") or "").strip()
    if not sku:
        raise HTTPException(400, "SKU (part number) is required.")
    kit.sku = sku
    kit.name = (form.get("name") or "").strip() or None
    kit.price = _parse_price(form.get("price") or "")
    kit.notes = (form.get("notes") or "").strip() or None
    reviewer = (current_user(request) or {}).get("email")
    _sync_kit_links(db, kit, form.getlist("forklift_ids"), reviewer)
    db.commit()
    return RedirectResponse(url="/#kits", status_code=303)


@app.post("/kit/{kid}/delete")
def delete_kit(kid: int, db: Session = Depends(get_db)):
    kit = db.get(Kit, kid)
    if kit:
        db.delete(kit)
        db.commit()
    return RedirectResponse(url="/#kits", status_code=303)


# ----------------------------------------------------------------------------
# Review workflow (admin only — enforced by auth_guard)
# ----------------------------------------------------------------------------
@app.post("/forklift/{fid}/status")
def set_forklift_status(fid: int, status: str = Form(...), db: Session = Depends(get_db)):
    """Set a forklift's info status: green | yellow | red."""
    fk = db.get(Forklift, fid)
    if not fk:
        raise HTTPException(404, "Forklift not found")
    if status in ("green", "yellow", "red"):
        fk.info_status = status
        db.commit()
    return RedirectResponse(url=f"/forklift/{fid}", status_code=303)


@app.post("/connection/{kit_id}/{forklift_id}/tier")
def set_connection_tier(kit_id: int, forklift_id: int, request: Request,
                        tier: str = Form(...), technician_name: str = Form(""),
                        install_date: str = Form(""), return_to: str = Form("/#kits"),
                        db: Session = Depends(get_db)):
    """Set a kit↔forklift connection's tier. Gold requires technician + date."""
    link = db.get(KitForklift, (kit_id, forklift_id))
    if not link:
        raise HTTPException(404, "Connection not found")
    if tier not in ("bronze", "silver", "gold"):
        raise HTTPException(400, "Invalid tier")
    if tier == "gold" and not (technician_name.strip() and install_date.strip()):
        raise HTTPException(400, "Gold requires a technician name and install date.")
    link.tier = tier
    link.reviewed_by = (current_user(request) or {}).get("email")
    link.technician_name = technician_name.strip() or None if tier == "gold" else None
    link.install_date = install_date.strip() or None if tier == "gold" else None
    db.commit()
    return RedirectResponse(url=return_to or "/#kits", status_code=303)


# ----------------------------------------------------------------------------
# User management (admin only; enforced by the auth_guard middleware)
# ----------------------------------------------------------------------------
@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.role, User.email).all()
    return templates.TemplateResponse(request, "users.html", _ctx(request, users=users))


@app.post("/users/{uid}/role")
def set_user_role(uid: int, request: Request, role: str = Form(...),
                  db: Session = Depends(get_db)):
    user = db.get(User, uid)
    me = current_user(request) or {}
    if user and role in ("admin", "viewer"):
        # Don't let an admin demote themselves (avoid locking out the last admin).
        if user.email == me.get("email") and role != "admin":
            raise HTTPException(400, "You cannot remove your own admin role.")
        user.role = role
        db.commit()
    return RedirectResponse(url="/users", status_code=303)


@app.post("/users/{uid}/delete")
def delete_user(uid: int, request: Request, db: Session = Depends(get_db)):
    user = db.get(User, uid)
    me = current_user(request) or {}
    if user and user.email != me.get("email"):
        db.delete(user)
        db.commit()
    return RedirectResponse(url="/users", status_code=303)


# ----------------------------------------------------------------------------
# PDF on request: link + live download, plus optional permanent archive
# ----------------------------------------------------------------------------
@app.get("/forklift/{fid}/pdf")
def get_pdf(fid: int, db: Session = Depends(get_db)):
    """Serve the locally archived PDF if saved; otherwise redirect the browser to
    the spec-sheet link. We never proxy remote content as a PDF — many sources
    403 the server or return HTML, which would produce a broken 'PDF'. The
    browser handles the real link (PDF or page) directly."""
    fk = db.get(Forklift, fid)
    if not fk:
        raise HTTPException(404, "Forklift not found")

    if fk.pdf_path and (PDF_DIR / Path(fk.pdf_path).name).exists():
        path = PDF_DIR / Path(fk.pdf_path).name
        return Response(path.read_bytes(), media_type="application/pdf")

    target = fk.pdf_url or fk.source_url
    if not target:
        raise HTTPException(404, "No spec-sheet link on this record.")
    return RedirectResponse(target, status_code=307)


@app.post("/forklift/{fid}/archive-pdf")
def archive_pdf(fid: int, db: Session = Depends(get_db)):
    """Save a permanent local copy of the PDF for this record."""
    fk = db.get(Forklift, fid)
    if not fk:
        raise HTTPException(404, "Forklift not found")
    target = fk.pdf_url or fk.source_url
    if not target:
        raise HTTPException(404, "No PDF or source link on this record.")
    try:
        r = httpx.get(target, follow_redirects=True, timeout=30)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Could not fetch the PDF: {e}")

    fname = f"{fid}_{fk.manufacturer}_{fk.model}.pdf".replace(" ", "_").replace("/", "-")
    (PDF_DIR / fname).write_bytes(r.content)
    fk.pdf_path = fname
    db.commit()
    return RedirectResponse(url=f"/forklift/{fid}", status_code=303)


# ----------------------------------------------------------------------------
# Plain-English question -> safe SQL -> answer
# ----------------------------------------------------------------------------
@app.post("/ask", response_class=HTMLResponse)
def ask(request: Request, question: str = Form(...), db: Session = Depends(get_db)):
    if not settings.ai_enabled:
        result = nl_query.NLQueryResult(
            question=question,
            error="AI is disabled. Add ANTHROPIC_API_KEY to .env to enable questions.",
        )
    else:
        result = nl_query.ask(db, question)
    return templates.TemplateResponse(
        request, "partials/ask_result.html", _ctx(request, result=result)
    )
