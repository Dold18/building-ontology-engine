"""Public scorer interface for owner implementations; tests use synthetic labels only."""
from typing import List, Literal, Optional
from pydantic import Field
from .models import Model

INVERSES = {'isFedBy': 'feeds', 'isPointOf': 'hasPoint', 'isPartOf': 'hasPart'}

class Edge(Model):
    building_id: str
    subject_id: str
    predicate: Literal['feeds', 'isFedBy', 'hasPoint', 'isPointOf', 'hasPart', 'isPartOf', 'type']
    object_id: str

    def key(self):
        s, p, o = self.subject_id, self.predicate, self.object_id
        if p in INVERSES: s, p, o = o, INVERSES[p], s
        return self.building_id, s, p, o

class LabeledEdge(Edge):
    label: Literal['true', 'false', 'unknown']
    label_source: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)

class PredictedEdge(Edge):
    disposition: Literal['accepted', 'abstained']

class EvaluationInput(Model):
    schema_version: Literal['evaluation-v1']
    scope: Literal['synthetic-development', 'owner-independent']
    dataset_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    graph_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    labels: List[LabeledEdge]
    predictions: List[PredictedEdge]

class Score(Model):
    building_id: str
    predicate: str
    true_positive: int
    false_accepted: int
    false_negative: int
    accepted_unknown: int
    accepted_unlabeled: int
    abstained_labeled: int
    precision: Optional[float]
    recall: Optional[float]
    coverage: Optional[float]
    abstention_rate: Optional[float]

class EvaluationOutput(Model):
    schema_version: Literal['evaluation-v1']
    scope: str
    dataset_sha256: str
    graph_sha256: str
    scores: List[Score]
    independent_verification: str = 'PENDING OWNER VERIFICATION'


def score(value):
    """Counts only adjudicated true/false for precision; unknown never means false."""
    value = EvaluationInput.model_validate(value)
    labels = {}
    for edge in value.labels:
        if edge.key() in labels: raise ValueError('DUPLICATE_LABEL: canonical/inverse key must be unique')
        labels[edge.key()] = edge.label
    predictions = {}
    for edge in value.predictions:
        key = edge.key()
        if key in predictions and predictions[key] != edge.disposition:
            raise ValueError('CONFLICTING_PREDICTION')
        predictions[key] = edge.disposition
    groups = sorted({(k[0], k[2]) for k in set(labels) | set(predictions)})
    scores = []
    ratio = lambda n, d: n / d if d else None
    for building, predicate in groups:
        keys = {k for k in set(labels) | set(predictions) if (k[0], k[2]) == (building, predicate)}
        known = {k for k in keys if labels.get(k) in {'true', 'false'}}
        accepted = {k for k in keys if predictions.get(k) == 'accepted'}
        tp = sum(labels.get(k) == 'true' for k in accepted)
        fp = sum(labels.get(k) == 'false' for k in accepted)
        positive = sum(labels.get(k) == 'true' for k in keys)
        abstained = len(known - accepted)  # omitted predictions also abstain
        scores.append(Score(building_id=building, predicate=predicate, true_positive=tp,
            false_accepted=fp, false_negative=positive-tp,
            accepted_unknown=sum(labels.get(k) == 'unknown' for k in accepted),
            accepted_unlabeled=sum(k not in labels for k in accepted), abstained_labeled=abstained,
            precision=ratio(tp,tp+fp), recall=ratio(tp,positive),
            coverage=ratio(len(known & accepted),len(known)), abstention_rate=ratio(abstained,len(known))))
    return EvaluationOutput(schema_version='evaluation-v1', scope=value.scope,
        dataset_sha256=value.dataset_sha256, graph_sha256=value.graph_sha256, scores=scores)
