"""Evidence policy v1: deterministic eligibility, never model self-verification."""
from .vocabulary import POINT_NAMES, POINTS, equipment_class, canonical_point_class

VERSION = 'evidence-v1'


def eligibility(subject, predicate, obj, source_ids, sources, method, explanation):
    reasons = []
    if not explanation.strip(): reasons.append('EMPTY_EXPLANATION')
    if not source_ids: reasons.append('MISSING_CITATION')
    records = [sources[s] for s in source_ids if s in sources]
    if len(records) != len(source_ids): reasons.append('UNRESOLVED_CITATION')
    if any(r.parse_status != 'ok' for r in records): reasons.append('INVALID_SOURCE_RECORD')
    if not reasons:
        if predicate == 'type':
            kind, _, tag = subject.partition('/')
            expected_kind = 'equipment' if kind == 'equipment' else 'points' if kind == 'point' else 'unknown'
            identifier = 'equipment_id' if kind == 'equipment' else 'point_id'
            matching = [r for r in records if r.table_kind == expected_kind and r.normalized_fields.get(identifier) == tag]
            if not matching:
                reasons.append('WRONG_ENTITY_OR_RECORD_KIND')
            elif kind == 'equipment':
                supported = {equipment_class(r.normalized_fields.get('equipment_type', '')) for r in matching}
                if obj not in supported: reasons.append('CLASS_NOT_SUPPORTED_BY_METADATA')
            elif obj != 'Point':
                supported = set()
                for r in matching:
                    f = r.normalized_fields
                    explicit = canonical_point_class(f.get('point_class', ''))
                    supported.add(explicit if explicit in POINTS else POINT_NAMES.get(f.get('description', '').lower(), 'unknown'))
                if obj not in supported or obj == 'unknown': reasons.append('CLASS_NOT_SUPPORTED_BY_METADATA')
        elif predicate == 'hasPoint':
            equipment = subject.removeprefix('equipment/')
            point = obj.removeprefix('point/')
            if not any(r.table_kind == 'points' and r.normalized_fields.get('point_id') == point
                       and r.normalized_fields.get('equipment_id') == equipment for r in records):
                reasons.append('OWNERSHIP_NOT_DOCUMENTED')
        elif predicate in {'feeds', 'hasPart'}:
            if method == 'llm': reasons.append('MODEL_TOPOLOGY_REQUIRES_REVIEW')
            if not any(r.table_kind == 'connections'
                and r.normalized_fields.get('from_equipment') == subject.removeprefix('equipment/')
                and r.normalized_fields.get('to_equipment') == obj.removeprefix('equipment/')
                and r.normalized_fields.get('predicate', 'feeds').removeprefix('brick:') == predicate for r in records):
                reasons.append('DIRECTION_OR_CONNECTION_NOT_DOCUMENTED')
        else:
            reasons.append('UNSUPPORTED_CLAIM_KIND')
    return {'policy_version': VERSION, 'eligible': not reasons, 'reason_codes': reasons or ['DOCUMENTED_METADATA'],
            'authority': 'supplied_table_unconfirmed', 'can_verify': False}
