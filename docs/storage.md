# Workspace storage

CRM data lives in a workspace folder as plain JSON, one file per record. This page describes `CRMStore` (`src/synapse_crm/store.py`), which the server does not use yet; the server still runs on Upjack. The store keeps records and the relationship index in the layout Upjack uses, so each can read records the other wrote. The checks and guarantees below are the store's own.

The server takes the workspace root from `UPJACK_ROOT`, then `MPAK_WORKSPACE`, then `./workspace`. A relative path resolves against the directory the server starts in.

## Layout

Paths are relative to the workspace root.

| Data | Path |
| --- | --- |
| Contacts | `apps/crm/data/contacts/<ct_id>.json` |
| Deals | `apps/crm/data/deals/<dl_id>.json` |
| Interactions | `apps/crm/data/interactions/<ix_id>.json` |
| Relationship index (derived) | `apps/crm/data/_index/relations.json` |
| Added fields (store only) | `apps/crm/schemas/<entity>.fields.json` |

## Records

An ID is the kind's prefix (`ct`, `dl` or `ix`), an underscore and 26 characters of uppercase Crockford base32. New IDs are ULIDs. The store refuses any other ID before it builds a path, and refuses any path that resolves outside the workspace root, including through a symlink.

Before each create or update, the store validates the whole record against the kind's schema in the app package's `schemas/` folder (not the workspace's), together with the base record fields from `entity.schema.json` in that folder and any added fields. Fields the schema does not list are kept. The store writes the record to a temporary file in the same folder, syncs it to disk and renames it over the record file, so a reader never sees a partial file. An existing file keeps its permissions.

Reading fills schema defaults in memory and never rewrites the file. An update reads the record, defaults included, replaces each given top-level field and saves the result, so passing `relationships` replaces the whole array. `id`, `type`, `version`, `created_at` and `created_by` never change after create.

Listing, search and relationship reads skip a file in a record folder whose name is not a valid ID or whose contents are not valid JSON. A file with a valid ID name that holds JSON other than an object, such as `[]`, is not skipped: the read fails.

## One writer per workspace

Run one `CRMStore` that writes to a workspace at a time. Its lock covers only calls on that one store, so a second store on the same workspace, in this process or another, is a second writer. The rename keeps files whole, but two writers at once can lose an update to the same record, drop one of two fields added at the same moment, or leave the index missing a link until the next rebuild.

## Relationship index

The record files are the source of truth for links: each record's `relationships` array holds entries with a `rel`, a `target` ID and an optional `label`. `relations.json` is derived from them as `{"reverse": {"<target id>": [{"source": "<id>", "rel": "<rel>"}]}}`, the records that link to each record. Every store create, update and delete rewrites it from all record files, and `rebuild_index` does the same on request. The store's own reverse lookups build this map from the record files on each call and never read the file, so a stale or missing index does not change their answers. Upjack's reverse lookups do read `relations.json` and rebuild it only when the file is missing, so in the server today a stale index changes their answers.

## Added fields

`CRMStore.add_field` saves added fields in `apps/crm/schemas/<entity>.fields.json`, for example:

```json
{
  "version": 1,
  "fields": {
    "region": {
      "type": "string",
      "default": "west",
      "required": true,
      "description": "Sales region"
    }
  }
}
```

On load the store merges these fields into the kind's schema and lists the required ones under `required`. A record saved before a field was added reads its default, and a new record gets it. The store never changes the app's own `schemas/` files. If the app later ships a field with the same name, the app's definition wins. A fields file the store cannot use stops the store from loading. The error names the file, unless the file's path resolves outside the workspace root.

Upjack's `add_field` tool, which the server serves today, works differently: it writes the field into the app's own `schemas/<entity>.schema.json` and never reads a `.fields.json` file.

## Deleting

A soft delete sets `status` to `deleted` through an ordinary update, and the file stays. `list_entities` returns active records unless asked for another status, `search_entities` leaves deleted records out unless its filter names `status`, and `query_by_relationship` returns active records only. `get_related` and `get_composite` still return a soft-deleted record, with `status` set to `deleted`. A hard delete (`hard=True`) removes the file and rewrites the index. Links that other records hold to a hard-deleted record stay in their files, and reverse lookups on its ID still return those records.
