"""Offset pagination with a count query only when the page itself cannot answer it."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def _total(db: AsyncSession, stmt: Select[Any], offset: int, returned: int, size: int) -> int:
    # A short page (and not an empty page past the end) holds the last rows, so the total
    # is known without a second round trip. Otherwise count the filtered set once.
    if returned < size and (returned > 0 or offset == 0):
        return offset + returned
    counted = select(func.count()).select_from(stmt.order_by(None).subquery())
    return int(await db.scalar(counted) or 0)


async def page_scalars(
    db: AsyncSession, stmt: Select[Any], page: int, page_size: int
) -> tuple[list[Any], int]:
    """Run an ordered single-entity select for one page; return (items, total)."""
    offset = (page - 1) * page_size
    rows = list((await db.execute(stmt.offset(offset).limit(page_size))).scalars().all())
    return rows, await _total(db, stmt, offset, len(rows), page_size)


async def page_rows(
    db: AsyncSession, stmt: Select[Any], page: int, page_size: int
) -> tuple[list[Any], int]:
    """Like `page_scalars` for multi-entity selects; returns row tuples."""
    offset = (page - 1) * page_size
    rows = list((await db.execute(stmt.offset(offset).limit(page_size))).all())
    return rows, await _total(db, stmt, offset, len(rows), page_size)
