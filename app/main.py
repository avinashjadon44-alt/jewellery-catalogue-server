"""
FastAPI server for the shared jewellery catalogue.
Desktop clients keep working offline and sync with this server on demand.

Security: set the JEWELLERY_API_KEY environment variable and every catalogue
endpoint requires a matching X-API-Key header. If it is not set the API is
open -- fine on your own PC, NOT fine on a public server.
"""
import hmac
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from . import database as db

UPLOADS_DIR = db.UPLOADS_DIR
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

API_KEY = os.environ.get("JEWELLERY_API_KEY", "")

# Render always sets RENDER=true. A public server with no key would let anyone
# on the internet edit the catalogue, so refuse to start instead.
if os.environ.get("RENDER") == "true" and not API_KEY:
    raise RuntimeError(
        "JEWELLERY_API_KEY is not set. Add it under Environment in the Render "
        "dashboard, then redeploy. Without it anyone could edit your catalogue."
    )

# Known categories; any capitalisation is mapped to these. Unknown ones are kept as typed.
CATEGORIES = ["Earrings", "Necklace", "Pendant", "Nose Ring", "Other"]
_CANONICAL = {c.lower(): c for c in CATEGORIES}


def _canonical_category(category: str) -> str:
    category = category.strip()
    return _CANONICAL.get(category.lower(), category)

# Codes become file names, so keep them boring: no slashes, dots, or spaces.
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
ALLOWED_EXT = {".png", ".webp", ".jpg", ".jpeg"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if not API_KEY:
        print("WARNING: JEWELLERY_API_KEY is not set -- the API is open to anyone "
              "who can reach this server.")
    yield


app = FastAPI(title="Virtual Jewellery Catalogue Server", lifespan=lifespan)


def require_key(x_api_key: str = Header(default="")):
    if API_KEY and not hmac.compare_digest(x_api_key.encode(), API_KEY.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------------- helpers ----------------

def _check_code(code: str) -> None:
    if not CODE_RE.match(code):
        raise HTTPException(
            status_code=422,
            detail="code may only contain letters, digits, '-' and '_' (max 64 characters)",
        )


def _check_time(ts: Optional[str]) -> Optional[str]:
    if not ts:
        return None
    if not TIME_RE.match(ts):
        raise HTTPException(status_code=422, detail="updated_at must be 'YYYY-MM-DD HH:MM:SS'")
    return ts


async def _save_image(code: str, upload: UploadFile, tag: str = "") -> str:
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"Unsupported image type '{ext}'. Use PNG, WebP or JPEG.")
    data = await upload.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty image file")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image larger than 10 MB")
    filename = f"{code}{tag}{ext}"
    (UPLOADS_DIR / filename).write_bytes(data)
    return filename


def _remove_file(filename: Optional[str]) -> None:
    if filename:
        (UPLOADS_DIR / filename).unlink(missing_ok=True)


def _has_file(upload: Optional[UploadFile]) -> bool:
    return upload is not None and bool(upload.filename)


# ---------------- endpoints ----------------

@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Virtual Jewellery Catalogue Server",
        "auth_required": bool(API_KEY),
    }


@app.get("/designs", dependencies=[Depends(require_key)])
def list_designs(include_deleted: bool = Query(False)):
    return db.list_all(include_deleted)


@app.get("/designs/{code}", dependencies=[Depends(require_key)])
def get_design(code: str):
    row = db.get_by_code(code)
    if not row or row["is_deleted"]:
        raise HTTPException(status_code=404, detail="Design not found")
    return row


@app.post("/designs", dependencies=[Depends(require_key)])
async def create_design(
    name: str = Form(...),
    code: str = Form(...),
    category: str = Form(...),
    price: float = Form(0),
    description: str = Form(""),
    scale: float = Form(0.30),
    x_offset: int = Form(0),
    y_offset: int = Form(28),
    updated_at: Optional[str] = Form(None),
    image: UploadFile = File(...),
    image_right: Optional[UploadFile] = File(None),
):
    _check_code(code)
    ts = _check_time(updated_at)
    category = _canonical_category(category)
    if db.get_by_code(code):
        raise HTTPException(status_code=409, detail=f"Design {code} already exists (use PUT to update it)")

    left = await _save_image(code, image)
    right = await _save_image(code, image_right, "_R") if _has_file(image_right) else None

    db.add({
        "name": name, "code": code, "category": category, "price": price,
        "description": description, "image_filename": left,
        "image_filename_right": right, "scale": scale,
        "x_offset": x_offset, "y_offset": y_offset, "updated_at": ts,
    })
    return db.get_by_code(code)


@app.put("/designs/{code}", dependencies=[Depends(require_key)])
async def update_design(
    code: str,
    name: str = Form(...),
    category: str = Form(...),
    price: float = Form(0),
    description: str = Form(""),
    scale: float = Form(0.30),
    x_offset: int = Form(0),
    y_offset: int = Form(28),
    clear_right: bool = Form(False),
    updated_at: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    image_right: Optional[UploadFile] = File(None),
):
    _check_code(code)
    ts = _check_time(updated_at)
    category = _canonical_category(category)
    existing = db.get_by_code(code)
    if not existing:
        raise HTTPException(status_code=404, detail="Design not found")

    left_name = existing["image_filename"]
    right_name = existing["image_filename_right"]
    stale_files = []

    if _has_file(image):
        new_left = await _save_image(code, image)
        if new_left != left_name:
            stale_files.append(left_name)
        left_name = new_left

    if _has_file(image_right):
        new_right = await _save_image(code, image_right, "_R")
        if right_name and new_right != right_name:
            stale_files.append(right_name)
        right_name = new_right
    elif clear_right:
        if right_name:
            stale_files.append(right_name)
        right_name = None

    db.update(code, {
        "name": name, "category": category, "price": price,
        "description": description, "image_filename": left_name,
        "image_filename_right": right_name, "scale": scale,
        "x_offset": x_offset, "y_offset": y_offset, "updated_at": ts,
    })
    for f in stale_files:
        _remove_file(f)
    return db.get_by_code(code)


@app.delete("/designs/{code}", dependencies=[Depends(require_key)])
def delete_design(code: str, updated_at: Optional[str] = Query(None)):
    _check_code(code)
    ts = _check_time(updated_at)
    row = db.get_by_code(code)
    if not row:
        raise HTTPException(status_code=404, detail="Design not found")
    if not row["is_deleted"]:  # already deleted: leave the tombstone time alone
        db.soft_delete(code, ts)
    return {"status": "deleted", "code": code}


def _image_response(code: str, field: str):
    row = db.get_by_code(code)
    if not row or not row[field]:
        raise HTTPException(status_code=404, detail="Image not found")
    path = UPLOADS_DIR / row[field]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Image file missing on server")
    return FileResponse(path)


@app.get("/designs/{code}/image", dependencies=[Depends(require_key)])
def get_image(code: str):
    return _image_response(code, "image_filename")


@app.get("/designs/{code}/image_right", dependencies=[Depends(require_key)])
def get_image_right(code: str):
    return _image_response(code, "image_filename_right")