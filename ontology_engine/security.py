"""Snapshot-bound operator assertions; not authenticated sponsor consent or claim verification."""
from dataclasses import dataclass
from .identity import uid

@dataclass(frozen=True)
class Approval:
    snapshot_uid: str
    provider: str
    model: str
    source_hashes: tuple

    def allows(self, records, model):
        return self.provider == 'openai' and self.model == model and {
            r.source_hash for r in records if r.source_hash} <= set(self.source_hashes)

    def as_dict(self):
        return {'approval_uid': uid('approval', self.snapshot_uid, self.provider, self.model),
                'snapshot_uid': self.snapshot_uid, 'provider': self.provider, 'model': self.model,
                'authority': 'explicit_local_operator_assertion', 'sponsor_consent_authenticated': False}
