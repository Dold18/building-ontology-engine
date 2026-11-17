"""Trusted data-origin classification (owner-gate security review).

A caller's `data_origin` label is never trusted on its own. Origin is derived from the records themselves:
- any record whose source hash or file name matches the sponsor intake partition is `sponsor`, whatever the caller says;
- `synthetic` is granted only when the caller says so AND every record carries a synthetic file label AND the sponsor
  index loaded successfully;
- everything else is `unverified`, which the owner-decision gate treats exactly like sponsor data.
Request bodies get the same check: a body naming a sponsor file or hash is sponsor data.
"""
import json
from pathlib import Path

DEFAULT_PARTITION = Path(__file__).resolve().parents[2] / 'config' / 'sponsor' / 'intake_partition.json'
SYNTHETIC_PREFIXES = ('SYN', 'SYNTHETIC')


class SponsorIndex:
    def __init__(self, partition_path=None):
        self.hashes, self.names, self.loaded = set(), set(), False
        path = Path(partition_path or DEFAULT_PARTITION)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            for entry in data['files']:
                self.hashes.add(entry['sha256'].lower())
                self.names.add(Path(entry['path']).name)
            self.loaded = bool(self.hashes)
        except (OSError, ValueError, KeyError, TypeError):
            self.loaded = False

    def record_is_sponsor(self, record):
        return (record.source_hash or '').lower() in self.hashes or record.source_file in self.names

    def text_mentions_sponsor(self, text):
        return any(name in text for name in self.names) or any(h in text.lower() for h in self.hashes)


def is_synthetic_labeled(record):
    return record.source_file.upper().startswith(SYNTHETIC_PREFIXES)


def trusted_origin(records, declared, index):
    if any(index.record_is_sponsor(r) for r in records):
        return 'sponsor'
    if declared == 'synthetic' and index.loaded and records and all(is_synthetic_labeled(r) for r in records):
        return 'synthetic'
    return 'sponsor' if declared == 'sponsor' else 'unverified'


def trusted_body_origin(body_text, declared, index):
    if index.text_mentions_sponsor(body_text):
        return 'sponsor'
    if declared == 'synthetic' and index.loaded:
        return 'synthetic'
    return 'sponsor' if declared == 'sponsor' else 'unverified'
