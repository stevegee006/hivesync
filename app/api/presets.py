"""Filter presets. SPEC section 12's GET /api/filter-presets, plus editing.

Built-in presets are read-only through this API. They are re-seeded from
`app.filter_presets` at every startup, so an edit would silently revert on the
next restart and a delete would come back. Refusing is honest; letting someone
edit something that reappears tomorrow is not.

A preset in use by a job cannot be deleted. The database enforces that too, via
RESTRICT on the association table, but a 409 with the job names in it is a much
better answer than an integrity error.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models import FilterPreset, Job

router = APIRouter(tags=["filter-presets"])


class PresetRules(BaseModel):
    exclude: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)


class PresetRead(BaseModel):
    id: int
    name: str
    builtin: bool
    rules: PresetRules
    # How many jobs would break if this went away. Shown next to the delete
    # button rather than discovered by pressing it.
    used_by: int


class PresetWrite(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    rules: PresetRules = Field(default_factory=PresetRules)

    @field_validator("name")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A preset needs a name.")
        return cleaned


def to_read(preset: FilterPreset, used_by: int) -> PresetRead:
    rules = preset.rules or {}
    return PresetRead(
        id=preset.id,
        name=preset.name,
        builtin=preset.builtin,
        rules=PresetRules(
            exclude=list(rules.get("exclude") or []),
            include=list(rules.get("include") or []),
        ),
        used_by=used_by,
    )


def _usage(session: DbSession) -> dict[int, int]:
    counts: dict[int, int] = {}
    for job in session.scalars(select(Job)):
        for preset in job.filter_presets:
            counts[preset.id] = counts.get(preset.id, 0) + 1
    return counts


@router.get("/filter-presets", response_model=list[PresetRead])
def list_presets(_user: CurrentUser, session: DbSession) -> list[PresetRead]:
    counts = _usage(session)
    return [
        to_read(preset, counts.get(preset.id, 0))
        for preset in session.scalars(
            select(FilterPreset).order_by(FilterPreset.builtin.desc(), FilterPreset.name)
        )
    ]


@router.post("/filter-presets", response_model=PresetRead, status_code=status.HTTP_201_CREATED)
def create_preset(payload: PresetWrite, _user: CurrentUser, session: DbSession) -> PresetRead:
    if session.scalar(select(FilterPreset).where(FilterPreset.name == payload.name)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A preset called '{payload.name}' already exists.",
        )
    preset = FilterPreset(
        name=payload.name, builtin=False, rules=payload.rules.model_dump(exclude_defaults=False)
    )
    session.add(preset)
    session.commit()
    session.refresh(preset)
    return to_read(preset, 0)


def _editable(session: DbSession, preset_id: int) -> FilterPreset:
    preset = session.get(FilterPreset, preset_id)
    if preset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such preset.")
    if preset.builtin:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"'{preset.name}' is built in and is refreshed from the application "
                "at every startup, so a change here would be undone on the next "
                "restart. Copy it into a new preset and edit that instead."
            ),
        )
    return preset


@router.patch("/filter-presets/{preset_id}", response_model=PresetRead)
def update_preset(
    preset_id: int, payload: PresetWrite, _user: CurrentUser, session: DbSession
) -> PresetRead:
    preset = _editable(session, preset_id)
    clash = session.scalar(
        select(FilterPreset).where(FilterPreset.name == payload.name, FilterPreset.id != preset_id)
    )
    if clash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A preset called '{payload.name}' already exists.",
        )
    preset.name = payload.name
    preset.rules = payload.rules.model_dump(exclude_defaults=False)
    session.commit()
    session.refresh(preset)
    return to_read(preset, _usage(session).get(preset.id, 0))


@router.delete("/filter-presets/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_preset(preset_id: int, _user: CurrentUser, session: DbSession) -> None:
    preset = _editable(session, preset_id)
    users = [job.name for job in session.scalars(select(Job)) if preset in job.filter_presets]
    if users:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"'{preset.name}' is used by {', '.join(sorted(users))}. Remove it "
                "from those jobs first: deleting it would change what they sync."
            ),
        )
    session.delete(preset)
    session.commit()
