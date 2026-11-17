# Frozen component association reference — SYNTHETIC / FOR TESTING ONLY

Supplementary development reference, separate from production inputs. Frozen before
implementing this revised association workflow. Original readiness_c fixtures,
reference and freeze stay exact as diagnostic history. Their proposed meters
predicate exceeds the accepted connection subset and must remain withheld; the
original expectation of its acceptance is superseded, not used as reference truth.
No accepted baseline evidence or prior tests may be changed to admit that relation.

These new source documents declare physical component associations instead:
HX-A hasPart Meter-A is supported; AHU-A hasPart Meter-Withheld is contradicted.
Meter-A is a declared Electrical_Meter and owns MeterPower. Therefore Meter-A is
associated as a documented component with HX-A only. This does not establish what
loads it measures or any electrical values. No meter readings are supplied.

All thermal/query expectations otherwise match the original development reference:
complete: AHU-A and HX-A have directed accepted feeds paths to Zone-A; Zone-Unknown
has only a pending branch; local thermal prerequisites ready, 41800 W at three UTC
instants (floating-point tolerance 1e-6 W), thermal comparison passes.
missing: no flow readings; not_evaluable, no calculated power, explicit insufficient
flow/alignment reasons. conflicted: opposing exclusive water circuit withholds
HX-A from accepted Zone-A answers; AHU-A remains. Local thermal readiness remains
ready because downstream service is not a prerequisite for the local water rule.
Unknown-A applicability is unknown; AHU-A and meters are outside that bounded rule.
An HVAC_Zone source row is typed only HVAC_Zone, never brick:Equipment; the legacy
schedule vocabulary variable/internal key does not determine RDF superclass truth.
