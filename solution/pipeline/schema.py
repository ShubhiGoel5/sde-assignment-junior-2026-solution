"""Loads the lead schemas and disposition list from the provided files."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalize_header

DEFAULT_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    values: tuple[str, ...] = ()
    default: str | None = None
    header_keys: frozenset[str] = frozenset()   # cleaned name + aliases


@dataclass(frozen=True)
class WorkflowSchema:
    workflow_id: str
    fields: tuple[Field, ...]
    phone_field: str = field(default="")        # phone used as identity
    reference_field: str = field(default="")    # used as external_ref


def load_schema(path: Path) -> WorkflowSchema:
    doc = json.loads(path.read_text(encoding="utf-8"))
    fields = []
    for f in doc["fields"]:
        keys = {normalize_header(f["name"])} | {normalize_header(a) for a in f.get("aliases", [])}
        fields.append(Field(
            name=f["name"], type=f["type"], required=bool(f.get("required")),
            pii=bool(f.get("pii")), reference=bool(f.get("reference")),
            values=tuple(f.get("values", [])), default=f.get("default"),
            header_keys=frozenset(keys)))
    phones = [f.name for f in fields if f.type == "phone" and f.required]
    refs = [f.name for f in fields if f.reference]
    if len(phones) != 1 or len(refs) != 1:
        raise ValueError(f"{path.name}: need exactly one required phone field and one reference field")
    return WorkflowSchema(doc["workflow_id"], tuple(fields), phones[0], refs[0])


def load_schemas(schema_dir: Path) -> dict[str, WorkflowSchema]:
    schemas = {}
    for path in sorted(schema_dir.glob("*.json")):
        s = load_schema(path)
        schemas[s.workflow_id] = s
    return schemas


def load_dispositions(schema_dir: Path) -> list[tuple[str, str, int, int]]:
    """Rows of (workflow_id, code, is_terminal, is_success)."""
    with open(schema_dir / "dispositions.csv", newline="", encoding="utf-8-sig") as f:
        return [(r["workflow_id"].strip(), r["code"].strip(),
                 int(r["is_terminal"].strip().lower() == "true"),
                 int(r["is_success"].strip().lower() == "true"))
                for r in csv.DictReader(f)]
