"""Identity v2: scoped logical claims, immutable source revisions, distinct observations."""
import hashlib
import json
from pathlib import Path

VERSION = 'identity-v2'


def uid(kind, *parts):
    value = json.dumps([VERSION, kind, *parts], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return kind + ':' + hashlib.sha256(value.encode()).hexdigest()


def building_scope(directory, explicit=None):
    # Default scope is an authorized input-root identity, not its mutable file contents.
    return explicit or uid('site', str(Path(directory).resolve()))


def unique_source_map(records):
    mapping = {}
    uids = set()
    for record in records:
        if record.source_id in mapping or record.source_record_uid in uids:
            raise ValueError('SOURCE_ID_COLLISION: duplicate source identity; processing refused')
        mapping[record.source_id] = record
        uids.add(record.source_record_uid)
    return mapping
