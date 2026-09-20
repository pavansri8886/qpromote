# QPromote Thesis: Fresh Experimental Results

## Evidence provenance

These values were generated from the current `qpromote.py` implementation on 20 September 2026, using a clean SQLite database `qpromote_fresh_evidence.db`. Results from the historical database were excluded.

- Pipeline run ID: `9a7b10fb`
- Repeated Stage 2 run ID: `f4f68730`
- Threshold scan run ID: `b05c5695`
- Shots per execution: 4096
- Qiskit: 2.5.2
- qiskit-aer: 0.17.2
- qiskit-ibm-runtime: 0.49.0
- Stage 2 backend: FakeManilaV2
- Stage 3 backend: FakeSherbrooke, used only as a hardware proxy

## Single fresh pipeline run

The fresh 15-stage run produced one record for each circuit and stage. Stage 3 was recorded as `SKIPPED` when Stage 2 was blocked; this is the configured promotion behavior and does not represent a hardware execution.

| Circuit | Stage 1 | Stage 2 | Stage 3 |
|---|---:|---:|---:|
| Bell | H=1.0000, PASS | H=0.9374, PASS | H=0.9624, DEMONSTRATION |
| GHZ | H=0.9995, PASS | H=0.8636, BLOCK | SKIPPED |
| Grover | H=1.0000, PASS | H=0.8987, BLOCK | SKIPPED |
| Bernstein-Vazirani | H=1.0000, PASS | H=0.9304, PASS | H=0.9724, DEMONSTRATION |
| QFT | H=1.0000, PASS | H=0.8660, BLOCK | SKIPPED |

The Stage 2 gate was Hellinger fidelity >= 0.90 and TVD <= 0.10. Two of five circuits passed the Stage 2 gate in this single run.

## Repeated Stage 2 experiment

Each circuit was executed 20 times at 4096 shots per execution.

| Circuit | Mean Hellinger | Standard deviation | Passes | Pass rate |
|---|---:|---:|---:|---:|
| Bell | 0.939960 | 0.003717 | 20/20 | 100% |
| Bernstein-Vazirani | 0.927515 | 0.003910 | 20/20 | 100% |
| GHZ | 0.869611 | 0.006219 | 0/20 | 0% |
| Grover | 0.897314 | 0.004958 | 6/20 | 30% |
| QFT | 0.855054 | 0.008053 | 0/20 | 0% |

The repeated experiment resolves the Grover boundary behavior: Grover is not consistently a pass or a block at the 0.90 threshold. It passed 6 of 20 executions, giving a 30% empirical pass rate.

Across the repeated experiment, 46 of 100 Stage 2 executions passed. This is not the same quantity as the single-run circuit-level promotion rate, which was 2 of 5 circuits.

## Threshold scan

The threshold scan contains 25 records: five circuits evaluated at thresholds 0.80, 0.85, 0.90, 0.925, and 0.95. The scan keeps the configured TVD limit of 0.10 and changes only the Hellinger threshold. Query the exact records with:

```bash
sqlite3 qpromote_fresh_evidence.db "SELECT circuit_name, threshold, hellinger, tvd, decision FROM threshold_scan WHERE run_id='b05c5695' ORDER BY circuit_name, threshold;"
```

Threshold-scan values should be copied into the thesis tables directly from this query rather than reconstructed from the older thesis draft.

## Interpretation and limitations

The repeated results support three conclusions. First, Bell and Bernstein-Vazirani are robust under the selected Stage 2 noise model. Second, GHZ and QFT remain below the quality gate across all repeated executions. Third, Grover is threshold-sensitive and should be described as probabilistic at the selected gate, not categorically stable.

These results are simulator and calibration-proxy evidence. `FakeManilaV2` models noise from a backend calibration snapshot, while `FakeSherbrooke` is not a live quantum processor. The results therefore demonstrate the promotion workflow and its quality-gate behavior; they do not establish performance on a real QPU or over time-varying hardware conditions.
