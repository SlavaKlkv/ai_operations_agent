"""Отдавать локальный одностраничный интерфейс."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).with_name("static")

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=FileResponse)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
