# Frozen valid panel association reference — SYNTHETIC / FOR TESTING ONLY

Frozen before final source association implementation. Original readiness_c and
readiness_c_components inputs/reference/freezes remain exact diagnostic history.
The original meters predicate is outside the accepted connection subset. The
component draft's Heat_Exchanger hasPart Electrical_Meter violates released Brick
HVAC equipment composition restrictions; that draft must fail SHACL/readiness.
Neither predicate support nor released shapes will be weakened to accept it.

Final positive association: generic Equipment Panel-A hasPart Electrical_Meter
Meter-A, directly documented by a synthetic panel component schedule. This is
physical panel membership only, not the loads measured. Meter-A owns MeterPower;
no electrical readings exist. Meter-A must answer Panel-A's meter query. HX-A has
no supported meter association and must return missing, not borrow Panel-A's meter.
AHU-A's proposed Meter-Withheld component remains contradicted and excluded.

Zone-A: AHU-A directly feeds it; HX-A reaches it via AHU-A in complete/missing only.
The conflicted case withholds HX-A -> AHU-A due opposing exclusive water directions.
Zone-Unknown: pending branch, no accepted service. Points Tin/Tout/Flow/Heat are
owned by HX-A. Generic Panel-A and Unknown-A applicability to the local thermal rule
is unknown; AHU-A and meters are outside the bounded heat-exchanger rule.

complete/conflicted thermal: supplied density 1000 kg/m3, cp 4180 J/(kg*K), flow
1 L/s, Tin 20 degC, Tout 30 degC yield 41800 W at three aligned UTC samples (absolute
floating tolerance 1e-6 W), compared against supplied 41.8 kW thermal power. ready,
screening pass. Downstream service is not a prerequisite for this local equation.
missing: no flow readings; not_evaluable, no calculation, exact insufficient-flow
and timestamp-alignment reasons. These are developer arithmetic inputs, not field
telemetry, sponsor facts, performance measurements, demand, energy or savings.
