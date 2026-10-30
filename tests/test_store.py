"""Workspace compatibility and write boundaries for the CRM record store."""

import json
from copy import deepcopy
from pathlib import Path
from threading import Event, Thread

import pytest
from jsonschema import ValidationError

from synapse_crm.store import CRMStore
from synapse_crm.tools.contact_timeline import contact_timeline
from synapse_crm.tools.log_interaction import log_interaction

ROOT = Path(__file__).resolve().parents[1]
INDEX = "apps/crm/data/_index/relations.json"


def test_existing_workspace_reads_preserve_bytes_and_custom_fields(tmp_path):
    fixture = json.loads((ROOT / "tests/fixtures/workspace.json").read_text())
    for relative, record in fixture.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2) + "\n")
    original = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    store = CRMStore(tmp_path, ROOT)
    contact = store.list_entities("contact")[0]
    assert contact["custom_field"] == "retained"
    assert {path: path.read_bytes() for path in original} == original
    updated = store.update_entity("contact", contact["id"], {"company": "Updated Co"})
    restarted = CRMStore(tmp_path, ROOT).get_entity("contact", contact["id"])
    assert restarted == updated
    assert restarted["custom_field"] == "retained"
    assert restarted["id"] == contact["id"]
    assert restarted["created_at"] == contact["created_at"]
    assert json.loads((tmp_path / INDEX).read_text()) == fixture[INDEX]


def test_invalid_updates_leave_saved_record_unchanged(tmp_path):
    store = CRMStore(tmp_path, ROOT)
    deal = store.create_entity("deal", {"title": "Original", "value": 20})
    path = tmp_path / "apps/crm/data/deals" / f"{deal['id']}.json"
    before = path.read_bytes()
    with pytest.raises(ValidationError):
        store.update_entity("deal", deal["id"], {"value": -1})
    assert path.read_bytes() == before
    assert store.get_entity("deal", deal["id"]) == deal


@pytest.mark.parametrize(
    "invalid",
    ["../../../../../outside", "dl_../../outside", "ct_" + "0" * 26, "dl_" + "Z" * 27],
)
def test_invalid_ids_never_become_paths(tmp_path, invalid):
    store = CRMStore(tmp_path / "workspace", ROOT)
    attempts = [
        lambda: store.create_entity("deal", {"id": invalid, "title": "X", "value": 1}),
        lambda: store.get_entity("deal", invalid),
        lambda: store.update_entity("deal", invalid, {"title": "Changed"}),
        lambda: store.delete_entity("deal", invalid),
        lambda: store.delete_entity("deal", invalid, hard=True),
    ]
    for attempt in attempts:
        with pytest.raises(ValueError):
            attempt()
    assert list(tmp_path.rglob("*")) == []


def test_symlinked_record_directory_cannot_leave_workspace(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    deals = tmp_path / "workspace/apps/crm/data/deals"
    deals.parent.mkdir(parents=True)
    deals.symlink_to(outside, target_is_directory=True)
    store = CRMStore(tmp_path / "workspace", ROOT)
    with pytest.raises(ValueError):
        store.create_entity("deal", {"title": "Escape", "value": 1})
    assert list(outside.iterdir()) == []


def test_soft_delete_hides_record_and_hard_delete_removes_file(tmp_path):
    store = CRMStore(tmp_path, ROOT)
    kept = store.create_entity("deal", {"title": "Kept", "value": 10})
    soft = store.create_entity("deal", {"title": "Soft", "value": 20})
    hard = store.create_entity("deal", {"title": "Hard", "value": 30})
    assert store.delete_entity("deal", soft["id"])["status"] == "deleted"
    assert store.delete_entity("deal", hard["id"], hard=True) == hard
    with pytest.raises(FileNotFoundError):
        store.get_entity("deal", hard["id"])
    assert [deal["id"] for deal in store.list_entities("deal")] == [kept["id"]]
    assert [deal["id"] for deal in store.list_entities("deal", status="deleted")] == [
        soft["id"]
    ]
    assert [deal["id"] for deal in store.search_entities("deal")] == [kept["id"]]


def test_search_applies_query_filters_and_sort(tmp_path):
    store = CRMStore(tmp_path, ROOT)
    small = store.create_entity("deal", {"title": "Small renewal", "value": 10})
    large = store.create_entity("deal", {"title": "Large renewal", "value": 90})
    store.create_entity("deal", {"title": "Other", "value": 50, "tags": ["hot"]})
    (tmp_path / "apps/crm/data/deals/notes.json").write_text("{}\n")
    found = store.search_entities("deal", query="RENEWAL", sort="value")
    assert [deal["id"] for deal in found] == [small["id"], large["id"]]
    found = store.search_entities("deal", filter={"value": {"$gte": 50}})
    assert {deal["title"] for deal in found} == {"Large renewal", "Other"}
    found = store.search_entities("deal", filter={"tags": {"$contains": "hot"}})
    assert [deal["title"] for deal in found] == ["Other"]
    assert len(store.search_entities("deal", limit=None)) == 3
    with pytest.raises(ValueError):
        store.search_entities("deal", filter={"value": {"$regex": "."}})


def test_upjack_reads_store_records_and_store_reads_upjack_records(tmp_path, app):
    store = CRMStore(tmp_path, ROOT)
    from_store = store.create_entity("contact", {"name": "Written by store"})
    assert app.get_entity("contact", from_store["id"])["name"] == "Written by store"
    from_upjack = app.create_entity("contact", {"name": "Written by Upjack"})
    assert store.get_entity("contact", from_upjack["id"])["name"] == "Written by Upjack"
    contacts = tmp_path / "apps/crm/data/contacts"
    path = contacts / f"{from_upjack['id']}.json"
    assert (contacts / f"{from_store['id']}.json").stat().st_mode == path.stat().st_mode
    path.chmod(0o640)
    store.update_entity("contact", from_upjack["id"], {"company": "Example Co"})
    assert app.get_entity("contact", from_upjack["id"])["company"] == "Example Co"
    assert path.stat().st_mode & 0o777 == 0o640


def test_store_writes_keep_upjack_relationship_queries_whole(tmp_path, app):
    store = CRMStore(tmp_path, ROOT)
    first = app.create_entity("contact", {"name": "First"})
    second = app.create_entity("contact", {"name": "Second"})
    early = log_interaction(app, first["id"], "note", "Before the store write")
    store.update_entity("contact", second["id"], {"company": "Example Co"})
    late = log_interaction(app, second["id"], "note", "After the store write")
    assert contact_timeline(app, first["id"])["total"] == 1
    store.update_entity("contact", second["id"], {"company": "Other Co"})
    app.delete_entity("interaction", late["id"], hard=True)
    assert contact_timeline(app, first["id"])["total"] == 1
    store.delete_entity("interaction", early["id"], hard=True)
    assert contact_timeline(app, first["id"])["total"] == 0


def test_immutable_fields_and_caller_values_are_preserved(tmp_path):
    store = CRMStore(tmp_path, ROOT)
    supplied = {"name": "Example contact", "tags": ["example"]}
    original = deepcopy(supplied)
    contact = store.create_entity("contact", supplied)
    assert supplied == original
    assert contact["created_at"] == contact["updated_at"]
    updated = store.update_entity(
        "contact",
        contact["id"],
        {"id": "different", "created_at": "invalid", "company": "Example"},
    )
    assert updated["id"] == contact["id"]
    assert updated["created_at"] == contact["created_at"]


def _ids(records):
    return sorted(record["id"] for record in records)


def _composite(reader, record):
    composite = reader.get_composite(record["type"], record["id"])
    related = composite["_related"]
    composite["_related"] = {name: _ids(linked) for name, linked in related.items()}
    return composite


def test_relationship_reads_match_upjack(tmp_path, app):
    store = CRMStore(tmp_path, ROOT)
    contact = app.create_entity("contact", {"name": "Example contact"})
    removed = app.create_entity("contact", {"name": "Removed contact"})
    open_deal, closed_deal, orphan_deal = (
        app.create_entity(
            "deal",
            {"title": title, "relationships": [{"rel": "belongs_to", "target": to}]},
        )
        for title, to in [
            ("Open", contact["id"]),
            ("Closed", contact["id"]),
            ("Orphan", removed["id"]),
        ]
    )
    note = log_interaction(app, contact["id"], "note", "Example note")
    app.delete_entity("deal", closed_deal["id"])
    app.delete_entity("contact", removed["id"], hard=True)

    linked = store.query_by_relationship("deal", "belongs_to", contact["id"])
    assert _ids(linked) == [open_deal["id"]]
    assert linked == app.query_by_relationship("deal", "belongs_to", contact["id"])
    other = {"title": "Other"}
    assert store.query_by_relationship("deal", "belongs_to", contact["id"], other) == []
    reverse = store.get_related(contact["id"], direction="reverse")
    assert _ids(reverse) == _ids([open_deal, closed_deal, note])
    assert store.get_related(contact["id"], rel="owns", direction="reverse") == []
    assert _ids(store.get_related(note["id"], rel="belongs_to")) == [contact["id"]]
    assert store.get_related(note["id"], rel="owns") == []
    orphaned = store.get_related(removed["id"], direction="reverse")
    assert _ids(orphaned) == [orphan_deal["id"]]
    for record in [contact, open_deal, closed_deal, orphan_deal, note]:
        for direction in ["forward", "reverse"]:
            assert _ids(store.get_related(record["id"], direction=direction)) == _ids(
                app.get_related(record["id"], direction=direction)
            )
        assert _composite(store, record) == _composite(app, record)


def test_reads_ignore_a_stale_index_and_rebuild_replaces_it(tmp_path):
    store = CRMStore(tmp_path, ROOT)
    contact = store.create_entity("contact", {"name": "Example contact"})
    link = [{"rel": "belongs_to", "target": contact["id"]}]
    deal = store.create_entity("deal", {"title": "Example", "relationships": link})
    index = tmp_path / INDEX
    index.write_text('{"reverse": {}}\n')
    assert store.get_related(contact["id"], direction="reverse") == [deal]
    composite = store.get_composite("contact", contact["id"])
    assert composite["_related"] == {"~belongs_to": [deal]}
    assert store.get_composite("contact", contact["id"], depth=0)["_related"] == {}
    assert store.rebuild_index() == {"success": True, "entries": 1}
    assert json.loads(index.read_text()) == {
        "reverse": {contact["id"]: [{"source": deal["id"], "rel": "belongs_to"}]}
    }
    store.delete_entity("deal", deal["id"], hard=True)
    assert store.rebuild_index() == {"success": True, "entries": 0}


def test_rebuild_cannot_publish_an_index_older_than_a_write(tmp_path, monkeypatch):
    store = CRMStore(tmp_path, ROOT)
    contact = store.create_entity("contact", {"name": "Example contact"})
    scanned, release = Event(), Event()
    scan = store._reverse

    def paused_scan():
        reverse = scan()
        if not scanned.is_set():
            scanned.set()
            release.wait(timeout=3)
        return reverse

    monkeypatch.setattr(store, "_reverse", paused_scan)
    rebuilder = Thread(target=store.rebuild_index)
    rebuilder.start()
    assert scanned.wait(timeout=3)
    link = [{"rel": "belongs_to", "target": contact["id"]}]
    data = {"title": "Concurrent", "relationships": link}
    writer = Thread(target=store.create_entity, args=("deal", data))
    writer.start()
    # Give the write time to finish first if the paused rebuild does not block it.
    writer.join(timeout=0.5)
    release.set()
    rebuilder.join(timeout=3)
    writer.join(timeout=3)
    reverse = json.loads((tmp_path / INDEX).read_text())["reverse"]
    assert [entry["rel"] for entry in reverse.get(contact["id"], [])] == ["belongs_to"]


def test_relationship_reads_reject_invalid_ids(tmp_path):
    store = CRMStore(tmp_path, ROOT)
    for invalid in ["dl_../../outside", "xx_" + "0" * 26]:
        for direction in ["forward", "reverse"]:
            with pytest.raises(ValueError):
                store.get_related(invalid, direction=direction)
    with pytest.raises(ValueError):
        store.get_related("dl_" + "0" * 26, direction="sideways")


REGION = {"type": "string", "default": "west", "required": True}
FIELDS = "apps/crm/schemas/contact.fields.json"


def test_added_field_lives_in_the_workspace_and_fills_older_records(tmp_path):
    installed = {path: path.read_bytes() for path in (ROOT / "schemas").iterdir()}
    store = CRMStore(tmp_path, ROOT)
    loaded_before = CRMStore(tmp_path, ROOT)
    older = store.create_entity("contact", {"name": "Older contact"})
    assert store.add_field("contact", "region", "string", "west", "Sales region") == {
        "success": True,
        "entity_type": "contact",
        "field": {"name": "region", **REGION},
    }
    assert store.add_field("contact", "tier", "string", "", required=False)["success"]
    assert {path: path.read_bytes() for path in installed} == installed
    assert store.get_entity("contact", older["id"])["region"] == "west"
    saved = (tmp_path / FIELDS).read_bytes()
    assert json.loads(saved) == {
        "version": 1,
        "fields": {
            "region": {**REGION, "description": "Sales region"},
            "tier": {"type": "string", "default": "", "required": False},
        },
    }
    restarted = CRMStore(tmp_path, ROOT)
    required = restarted._schemas["contact"]["required"]
    assert "region" in required
    assert "tier" not in required
    assert restarted.get_entity("contact", older["id"])["region"] == "west"
    created = restarted.create_entity("contact", {"name": "New contact"})
    assert created["region"] == "west"
    updated = restarted.update_entity("contact", older["id"], {"region": "east"})
    assert updated["region"] == "east"
    with pytest.raises(ValidationError):
        restarted.update_entity("contact", older["id"], {"region": 7})
    assert loaded_before.add_field("contact", "region", "integer", 0) == {
        "error": "Field 'region' already exists with type 'string'"
    }
    assert (tmp_path / FIELDS).read_bytes() == saved


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (("company", "region", "string", ""), "Unknown entity type 'company'"),
        (
            ("contact", "Region", "string", ""),
            "Invalid field_name 'Region'. Must match [a-z][a-z0-9_]*",
        ),
        (
            ("company", "Region", "string", ""),
            "Invalid field_name 'Region'. Must match [a-z][a-z0-9_]*",
        ),
        (("contact", "id", "string", ""), "Field 'id' is a reserved base entity field"),
        (
            ("contact", "region", "date", ""),
            "Invalid field_type 'date'. Allowed: "
            "['array', 'boolean', 'integer', 'number', 'object', 'string']",
        ),
        (
            ("contact", "score", "integer", "high"),
            "Default value 'high' is not compatible with type 'integer'",
        ),
        (
            ("contact", "score", "integer", True),
            "Default value True is not compatible with type 'integer'",
        ),
        (
            ("contact", "score", "number", True),
            "Default value True is not compatible with type 'number'",
        ),
        (("contact", "email", "string", ""), "Field 'email' already exists"),
    ],
)
def test_rejected_fields_write_nothing(tmp_path, arguments, error):
    store = CRMStore(tmp_path, ROOT)
    assert store.add_field(*arguments) == {"error": error}
    assert list(tmp_path.rglob("*")) == []


def test_a_field_the_app_later_ships_keeps_the_app_definition(tmp_path):
    path = tmp_path / FIELDS
    path.parent.mkdir(parents=True)
    shipped = {"type": "integer", "default": 0, "required": True}
    fields = {"email": shipped, "tags": shipped, "region": REGION}
    path.write_text(json.dumps({"version": 1, "fields": fields}))
    contact = CRMStore(tmp_path, ROOT).create_entity("contact", {"name": "Example"})
    assert "email" not in contact
    assert contact["tags"] == []
    assert contact["region"] == "west"


@pytest.mark.parametrize(
    "content",
    [
        b"{",
        b"\xff\xfe",
        {"version": 2, "fields": {"region": REGION}},
        {"version": True, "fields": {"region": REGION}},
        {"version": 1, "fields": {}, "extra": 1},
        {"version": 1, "fields": []},
        {"version": 1, "fields": {"region": {**REGION, "default": 7}}},
        {"version": 1, "fields": {"region": {**REGION, "required": "yes"}}},
        {"version": 1, "fields": {"region": {**REGION, "description": 7}}},
        {"version": 1, "fields": {"region": {**REGION, "enum": ["west"]}}},
    ],
)
def test_store_refuses_to_load_an_invalid_fields_file(tmp_path, content):
    path = tmp_path / FIELDS
    path.parent.mkdir(parents=True)
    path.write_bytes(
        content if isinstance(content, bytes) else json.dumps(content).encode()
    )
    with pytest.raises(ValueError, match="contact.fields.json"):
        CRMStore(tmp_path, ROOT)
