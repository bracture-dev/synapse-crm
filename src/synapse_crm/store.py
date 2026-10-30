"""CRM records in the existing workspace JSON format."""

import json
import os
import re
from collections.abc import Collection
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from jsonschema import Draft202012Validator
from ulid import ULID

_ENTITIES = {
    "contact": ("contacts", "ct"),
    "deal": ("deals", "dl"),
    "interaction": ("interactions", "ix"),
}
_IMMUTABLE = {"id", "type", "version", "created_at", "created_by"}
_BASE_SCHEMA_REF = "https://upjack.dev/schemas/v1/upjack-entity.schema.json"
_INDEX = "apps/crm/data/_index/relations.json"
_EXTENSIONS = "apps/crm/schemas/{kind}.fields.json"
_FIELD_TYPES = ("array", "boolean", "integer", "number", "object", "string")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _matches(entity: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    for field, expected in (filters or {}).items():
        value = entity.get(field)
        if not isinstance(expected, dict):
            if value != expected:
                return False
            continue
        for operator, operand in expected.items():
            if operator == "$exists":
                okay = (field in entity and value is not None) == operand
            elif operator == "$ne":
                okay = value != operand
            elif operator == "$in":
                okay = value in operand
            elif operator == "$contains":
                okay = isinstance(value, list) and operand in value
            elif operator in {"$gt", "$gte", "$lt", "$lte"}:
                try:
                    okay = (
                        value is not None
                        and {
                            "$gt": lambda: value > operand,
                            "$gte": lambda: value >= operand,
                            "$lt": lambda: value < operand,
                            "$lte": lambda: value <= operand,
                        }[operator]()
                    )
                except TypeError:
                    okay = False
            else:
                raise ValueError(f"Unsupported filter operator: {operator}")
            if not okay:
                return False
    return True


def _kind(entity_id: str) -> str:
    for kind, (_, prefix) in _ENTITIES.items():
        if entity_id.startswith(prefix + "_"):
            return kind
    raise ValueError("Unknown entity ID prefix")


def _field_error(reserved: Collection[str], name: str, field: Any) -> str | None:
    """Say why a field cannot be added, in Upjack's add_field words and order."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        return f"Invalid field_name '{name}'. Must match [a-z][a-z0-9_]*"
    if name in reserved:
        return f"Field '{name}' is a reserved base entity field"
    if (
        not isinstance(field, dict)
        or set(field) - {"description"} != {"type", "default", "required"}
        or not isinstance(field["required"], bool)
        or not isinstance(field.get("description", ""), str)
    ):
        return f"Invalid definition for field '{name}'"
    if field["type"] not in _FIELD_TYPES:
        return f"Invalid field_type '{field['type']}'. Allowed: {list(_FIELD_TYPES)}"
    # Records are validated with JSON Schema, where True is not an integer.
    if not Draft202012Validator({"type": field["type"]}).is_valid(field["default"]):
        return (
            f"Default value {field['default']!r} is not compatible with type "
            f"'{field['type']}'"
        )
    return None


def _load_schema(schemas: Path, kind: str, fields: dict[str, Any]) -> dict[str, Any]:
    schema = json.loads((schemas / f"{kind}.schema.json").read_text())
    base = json.loads((schemas / "entity.schema.json").read_text())
    # Swap the local base record schema in for the remote $ref only.
    schema["allOf"] = [
        base
        if isinstance(part, dict) and part.get("$ref") == _BASE_SCHEMA_REF
        else part
        for part in schema.get("allOf", [])
    ]
    for name, field in fields.items():
        # A field the app or the base record schema now defines keeps that
        # definition, so an app update that ships the same name still loads.
        if name in schema["properties"] or name in base["properties"]:
            continue
        error = _field_error(base["properties"], name, field)
        if error:
            relative = _EXTENSIONS.format(kind=kind)
            raise ValueError(f"Invalid field in {relative}: {error}")
        schema["properties"][name] = {
            key: value for key, value in field.items() if key != "required"
        }
        if field["required"]:
            schema.setdefault("required", []).append(name)
    return schema


class CRMStore:
    """CRM records stored as one JSON file each under the workspace root."""

    def __init__(self, root: str | Path, project_root: str | Path) -> None:
        """Load the entity schemas, with any fields added in the workspace.

        Args:
            root: Workspace root that holds ``apps/crm/data`` and
                ``apps/crm/schemas``.
            project_root: Repository root that holds ``schemas``.
        """
        self.root = Path(root).resolve()
        self._lock = RLock()
        self._schema_dir = Path(project_root).resolve() / "schemas"
        base = json.loads((self._schema_dir / "entity.schema.json").read_text())
        self._reserved = set(base["properties"])
        self._schemas = {
            kind: _load_schema(self._schema_dir, kind, self._read_extensions(kind))
            for kind in _ENTITIES
        }

    def _path(self, relative: str) -> Path:
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Path escapes workspace root")
        return path

    def _entity_path(self, kind: str, entity_id: str) -> Path:
        plural, prefix = _ENTITIES[kind]
        if not re.fullmatch(prefix + r"_[0-9A-HJKMNP-TV-Z]{26}", entity_id):
            raise ValueError(f"Invalid {kind} ID")
        return self._path(f"apps/crm/data/{plural}/{entity_id}.json")

    def _read_extensions(self, kind: str) -> dict[str, Any]:
        """Read the fields ``add_field`` saved in the workspace for one kind."""
        relative = _EXTENSIONS.format(kind=kind)
        path = self._path(relative)
        try:
            document = json.loads(path.read_text())
        except FileNotFoundError:
            return {}
        except ValueError as error:
            # Also catches UnicodeDecodeError, for bytes that are not UTF-8.
            raise ValueError(f"Invalid JSON in {relative}: {error}") from None
        # Python counts True and 1.0 as equal to 1; add_field writes neither.
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "fields"}
            or type(document["version"]) is not int
            or document["version"] != 1
            or not isinstance(document["fields"], dict)
        ):
            raise ValueError(f"Unsupported version or layout in {relative}")
        return document["fields"]

    @staticmethod
    def _atomic(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{ULID()}.tmp")
        # 0o666 lets the umask set a new file's mode, as a plain write does.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        try:
            with os.fdopen(fd, "w") as stream:
                if path.exists():
                    os.fchmod(stream.fileno(), path.stat().st_mode & 0o7777)
                json.dump(value, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _reverse(self) -> dict[str, list[dict[str, str]]]:
        """Build Upjack's reverse relationship index from every record file."""
        reverse: dict[str, list[dict[str, str]]] = {}
        for kind in _ENTITIES:
            for record in self._all(kind):
                for link in record.get("relationships", []):
                    if not link.get("target") or not link.get("rel"):
                        continue
                    entry = {"source": record.get("id", ""), "rel": link["rel"]}
                    sources = reverse.setdefault(link["target"], [])
                    if entry not in sources:
                        sources.append(entry)
        return reverse

    def _find(self, entity_id: str) -> dict[str, Any] | None:
        try:
            return self.get_entity(_kind(entity_id), entity_id)
        except (ValueError, FileNotFoundError):
            # A link to an unknown ID, or to a record that was hard deleted.
            return None

    def _save(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        Draft202012Validator(self._schemas[kind]).validate(record)
        self._atomic(self._entity_path(kind, record["id"]), record)
        self.rebuild_index()
        return deepcopy(record)

    def _hydrate(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        record = deepcopy(record)
        for schema in [*self._schemas[kind].get("allOf", []), self._schemas[kind]]:
            for field, definition in schema.get("properties", {}).items():
                if field not in record and "default" in definition:
                    record[field] = deepcopy(definition["default"])
        return record

    def _all(self, kind: str) -> list[dict[str, Any]]:
        directory = self._path(f"apps/crm/data/{_ENTITIES[kind][0]}")
        records = []
        for path in sorted(directory.glob("*.json")):
            try:
                records.append(self.get_entity(kind, path.stem))
            except (ValueError, FileNotFoundError):
                # Bad JSON, a name that is not an ID, or a file removed since the glob.
                continue
        return records

    def create_entity(
        self, kind: str, data: dict[str, Any], created_by: str = "agent"
    ) -> dict[str, Any]:
        """Validate and write a new record.

        Args:
            kind: One of contact, deal or interaction.
            data: Record fields. A supplied ``id`` must be a valid ID for the kind.
            created_by: Stored as ``created_by``.

        Returns:
            The saved record.
        """
        with self._lock:
            data = deepcopy(data)
            supplied_id = data.pop("id", None)
            entity_id = supplied_id or f"{_ENTITIES[kind][1]}_{ULID()}"
            path = self._entity_path(kind, entity_id)
            if path.exists():
                raise ValueError(f"Entity already exists: {entity_id}")
            stamp = _now_iso()
            record = {
                "version": 1,
                "created_at": stamp,
                "updated_at": stamp,
                "created_by": created_by,
                "status": "active",
                "tags": [],
                "relationships": [],
                **data,
                "id": entity_id,
                "type": kind,
            }
            return self._save(kind, self._hydrate(kind, record))

    def get_entity(self, kind: str, entity_id: str) -> dict[str, Any]:
        """Read one record, with schema defaults filled in memory only.

        Args:
            kind: One of contact, deal or interaction.
            entity_id: The record ID.

        Returns:
            The record.
        """
        with self._lock:
            return self._hydrate(
                kind, json.loads(self._entity_path(kind, entity_id).read_text())
            )

    def update_entity(
        self, kind: str, entity_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge fields into a record and validate the whole record before writing.

        Args:
            kind: One of contact, deal or interaction.
            entity_id: The record ID.
            data: Fields to set. Immutable fields are ignored.

        Returns:
            The saved record.
        """
        with self._lock:
            record = self.get_entity(kind, entity_id)
            record.update({k: v for k, v in data.items() if k not in _IMMUTABLE})
            record["updated_at"] = _now_iso()
            return self._save(kind, record)

    def list_entities(
        self, kind: str, status: str = "active", limit: int | None = None
    ) -> list[dict[str, Any]]:
        """List records with one status, most recently updated first.

        Args:
            kind: One of contact, deal or interaction.
            status: Status to match.
            limit: Maximum number of records, or None for all.

        Returns:
            The matching records.
        """
        records = [
            record
            for record in self._all(kind)
            if record.get("status", "active") == status
        ]
        ordered = sorted(
            records, key=lambda record: record.get("updated_at", ""), reverse=True
        )
        return ordered if limit is None else ordered[: max(0, limit)]

    def delete_entity(
        self, kind: str, entity_id: str, hard: bool = False
    ) -> dict[str, Any]:
        """Set a record's status to deleted, or remove its file when ``hard``.

        Args:
            kind: One of contact, deal or interaction.
            entity_id: The record ID.
            hard: Remove the file instead of marking the record deleted.

        Returns:
            The record as saved, or as it was before a hard delete.
        """
        with self._lock:
            record = self.get_entity(kind, entity_id)
            if hard:
                self._entity_path(kind, entity_id).unlink()
                self.rebuild_index()
                return record
            return self.update_entity(kind, entity_id, {"status": "deleted"})

    def search_entities(
        self,
        kind: str,
        query: str | None = None,
        filter: dict[str, Any] | None = None,
        sort: str = "-updated_at",
        limit: int | None = 20,
    ) -> list[dict[str, Any]]:
        """Search records by text and field filters.

        Args:
            kind: One of contact, deal or interaction.
            query: Case-insensitive text matched against string fields.
            filter: Field values or operators such as ``{"value": {"$gte": 50}}``.
                Deleted records are left out unless the filter names ``status``.
            sort: Field to sort by, with a leading ``-`` for descending.
            limit: Maximum number of records, or None for all.

        Returns:
            The matching records.
        """
        records = [
            record
            for record in self._all(kind)
            if ((filter and "status" in filter) or record.get("status") != "deleted")
            and (
                not query
                or any(
                    query.lower() in v.lower()
                    for v in record.values()
                    if isinstance(v, str)
                )
            )
            and _matches(record, filter)
        ]
        field = sort.lstrip("-")

        def sort_key(record: dict[str, Any]) -> tuple[bool, bool, Any]:
            value = record.get(field)
            return (
                value is not None,
                isinstance(value, str),
                value if value is not None else "",
            )

        ordered = sorted(records, key=sort_key, reverse=sort.startswith("-"))
        return ordered if limit is None else ordered[: max(0, limit)]

    def query_by_relationship(
        self,
        kind: str,
        rel: str,
        target_id: str,
        filter: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """List active records of one kind that link to a target.

        Args:
            kind: One of contact, deal or interaction.
            rel: Relationship type, such as ``belongs_to``.
            target_id: ID of the linked record.
            filter: Field values or operators, as in ``search_entities``.
            limit: Maximum number of records, or None for all.

        Returns:
            The matching records.
        """
        records = [
            record
            for record in self._all(kind)
            if record.get("status", "active") == "active"
            and any(
                link.get("rel") == rel and link.get("target") == target_id
                for link in record.get("relationships", [])
            )
            and _matches(record, filter)
        ]
        return records if limit is None else records[: max(0, limit)]

    def get_related(
        self, entity_id: str, rel: str | None = None, direction: str = "forward"
    ) -> list[dict[str, Any]]:
        """Follow relationship links from a record.

        Args:
            entity_id: The record ID.
            rel: Relationship type to follow, or None for all.
            direction: ``forward`` for the records this one links to, or
                ``reverse`` for the records that link to it. A reverse lookup
                still works after the record itself was hard deleted.

        Returns:
            The linked records. Links to missing records are skipped.
        """
        kind = _kind(entity_id)
        if direction == "forward":
            ids = [
                link["target"]
                for link in self.get_entity(kind, entity_id).get("relationships", [])
                if link.get("target") and (rel is None or link.get("rel") == rel)
            ]
        elif direction == "reverse":
            # Check the ID even though this lookup never opens its file.
            self._entity_path(kind, entity_id)
            ids = [
                entry["source"]
                for entry in self._reverse().get(entity_id, [])
                if rel is None or entry["rel"] == rel
            ]
        else:
            raise ValueError("Direction must be forward or reverse")
        return [record for record in map(self._find, ids) if record is not None]

    def get_composite(
        self, kind: str, entity_id: str, depth: int = 1
    ) -> dict[str, Any]:
        """Read a record with its linked records under ``_related``.

        Args:
            kind: One of contact, deal or interaction.
            entity_id: The record ID.
            depth: 0 leaves ``_related`` empty. Any higher value follows one
                hop, as Upjack does.

        Returns:
            The record, with the records it links to keyed by rel and the
            records that link to it keyed by ``~rel``.
        """
        entity = self.get_entity(kind, entity_id)
        related: dict[str, list[dict[str, Any]]] = {}
        if depth >= 1:
            links = [
                (link.get("rel"), link.get("target"))
                for link in entity.get("relationships", [])
            ] + [
                ("~" + entry["rel"], entry["source"])
                for entry in self._reverse().get(entity_id, [])
            ]
            for name, other_id in links:
                other = self._find(other_id) if name and other_id else None
                if other is not None:
                    related.setdefault(name, []).append(other)
        return {**entity, "_related": related}

    def rebuild_index(self) -> dict[str, Any]:
        """Rewrite Upjack's reverse relationship index from every record file.

        Returns:
            ``{"success": True, "entries": n}``, as Upjack's ``rebuild_index``
            tool returns.
        """
        # Scan and publish under the write lock, so a rebuild never replaces
        # an index that a newer write has already published.
        with self._lock:
            reverse = self._reverse()
            self._atomic(self._path(_INDEX), {"reverse": reverse})
            return {"success": True, "entries": sum(map(len, reverse.values()))}

    def add_field(
        self,
        entity_type: str,
        field_name: str,
        field_type: str,
        default: Any,
        description: str = "",
        required: bool = True,
    ) -> dict[str, Any]:
        """Add a field to one kind's schema, stored in the workspace.

        Takes the arguments of Upjack's ``add_field`` tool and runs its checks
        in the same order, but checks a default with the JSON Schema type rules
        that validate records. Writes
        ``apps/crm/schemas/<entity_type>.fields.json`` in the workspace and
        leaves the app's own schemas unchanged.

        Args:
            entity_type: One of contact, deal or interaction.
            field_name: Lowercase name that no base record field uses.
            field_type: array, boolean, integer, number, object or string.
            default: Value that records without the field read, and that new
                records get.
            description: Stored with the field when not empty.
            required: Whether every saved record must have the field.

        Returns:
            ``{"success": True, "entity_type": ..., "field": ...}``, or
            ``{"error": ...}`` with nothing written.
        """
        field = {"type": field_type, "default": deepcopy(default), "required": required}
        if description:
            field["description"] = description
        # Upjack checks the field before the kind; every kind reserves the same
        # base record fields.
        error = _field_error(self._reserved, field_name, field)
        if error is None and entity_type not in _ENTITIES:
            error = f"Unknown entity type '{entity_type}'"
        if error:
            return {"error": error}
        with self._lock:
            # Check against the saved file, which also holds fields that another
            # store on this workspace added after this one loaded.
            fields = self._read_extensions(entity_type)
            current = _load_schema(self._schema_dir, entity_type, fields)
            existing = current["properties"].get(field_name)
            if existing is not None:
                if existing.get("type") and existing["type"] != field_type:
                    return {
                        "error": f"Field '{field_name}' already exists with type "
                        f"'{existing['type']}'"
                    }
                return {"error": f"Field '{field_name}' already exists"}
            fields[field_name] = field
            schema = _load_schema(self._schema_dir, entity_type, fields)
            self._atomic(
                self._path(_EXTENSIONS.format(kind=entity_type)),
                {"version": 1, "fields": fields},
            )
            self._schemas[entity_type] = schema
        return {
            "success": True,
            "entity_type": entity_type,
            "field": {
                "name": field_name,
                "type": field_type,
                "default": default,
                "required": required,
            },
        }
