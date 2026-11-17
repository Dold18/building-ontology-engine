# Frozen developer reference — SYNTHETIC / FOR TESTING ONLY

This file is separate from application inputs and never read by production code.
All cases: Tin/Tout/Flow/Heat belong to HX-A; MeterPower belongs to Meter-A.
Meter-A meters HX-A only. Meter-Withheld -> AHU-A is contradicted and never an
accepted metering answer. Zone-Unknown has only a pending branch: unknown service.
Generic Unknown-A applicability is unknown. AHU-A is inapplicable to the water-side
heat-exchanger rule; this does not mean other HVAC analyses are inapplicable.

complete: AHU-A directly feeds Zone-A; HX-A reaches Zone-A through AHU-A.
Thermal calculation: 1000 kg/m3 * 4180 J/(kg*K) * 0.001 m3/s * 10 K =
41800 W at exactly three aligned UTC instants. Prerequisites ready; screening pass.
No electric meter telemetry is supplied: association cannot establish demand,
energy consumption, savings, or grid impact.

missing: same graph; all flow readings absent. Thermal result not_evaluable;
INSUFFICIENT_FLOW_SAMPLES and UNALIGNED_TIMESTAMPS expected. No calculated power.

conflicted: opposing exclusive water directions withhold HX-A -> AHU-A.
AHU-A still serves Zone-A; HX-A must not appear in accepted zone answers.
Thermal readiness for HX-A can remain ready: local documented point ownership,
not this downstream path, is required by the bounded water-side equation.
