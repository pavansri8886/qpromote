# QPromote Thesis: Fresh Experimental Results

## Evidence provenance

These values were generated from the current `qpromote.py` implementation on 20 September 2026, using a clean SQLite database `qpromote_fresh_evidence.db`. Results from the historical database were excluded.

- Pipeline run ID: `2b9337cc`
- Repeated Stage 2 run ID: `f588db0b`
- Threshold scan run ID: `cfdd6c42`
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
| Bell | H=0.9999, PASS | H=0.9390, PASS | H=0.9575, DEMONSTRATION |
| GHZ | H=0.9999, PASS | H=0.8720, BLOCK | SKIPPED |
| Grover | H=1.0000, PASS | H=0.8972, BLOCK | SKIPPED |
| Bernstein-Vazirani | H=1.0000, PASS | H=0.9312, PASS | H=0.9729, DEMONSTRATION |
| QFT | H=1.0000, PASS | H=0.8506, BLOCK | SKIPPED |

The Stage 2 gate was Hellinger fidelity >= 0.90 and TVD <= 0.10. Two of five circuits passed the Stage 2 gate in this single run.

## Repeated Stage 2 experiment

Each circuit was executed 20 times at 4096 shots per execution, using recorded simulator and transpiler seeds.

| Circuit | Mean Hellinger | Standard deviation | Passes | Pass rate |
|---|---:|---:|---:|---:|
| Bell | 0.938719 | 0.003554 | 20/20 | 100% |
| Bernstein-Vazirani | 0.927820 | 0.003367 | 20/20 | 100% |
| GHZ | 0.868675 | 0.005253 | 0/20 | 0% |
| Grover | 0.897424 | 0.004354 | 4/20 | 20% |
| QFT | 0.852576 | 0.005074 | 0/20 | 0% |

The repeated experiment resolves the Grover boundary behavior: Grover is not consistently a pass or a block at the 0.90 threshold. It passed 4 of 20 executions, giving a 20% empirical pass rate.

Across the repeated experiment, 44 of 100 Stage 2 executions passed. This is not the same quantity as the single-run circuit-level promotion rate, which was 2 of 5 circuits.

## Threshold scan

The threshold scan contains 500 records: five circuits, 20 sampled repetitions, and five thresholds (0.80, 0.85, 0.90, 0.925, and 0.95). Each repetition samples a circuit once; all five threshold decisions are then computed from that same measured distribution. The scan keeps the configured TVD limit of 0.10. Query the exact records with:

```bash
sqlite3 qpromote_fresh_evidence.db "SELECT circuit_name, repetition, threshold, hellinger, tvd, decision FROM threshold_scan WHERE run_id='cfdd6c42' ORDER BY circuit_name, repetition, threshold;"
```

The threshold scan found that Bell passed through 0.925, BV passed through 0.90, Grover passed at 0.80 and 0.85 but not at 0.90 or above, and GHZ/QFT failed at all tested thresholds because TVD remained above 0.10.

## Interpretation and limitations

The corrected implementation also uses an independent QFT expected distribution (`{"000": 1.0}`), counts quantum operations and measurements separately for CFP, and records raw counts, reference distributions, seeds, code commit, and configuration identity. The repeated results support three conclusions. First, Bell and Bernstein-Vazirani are robust under the selected Stage 2 noise model. Second, GHZ and QFT remain below the quality gate across all repeated executions. Third, Grover is threshold-sensitive and should be described as probabilistic at the selected gate, not categorically stable.

These results are simulator and calibration-proxy evidence. `FakeManilaV2` models noise from a backend calibration snapshot, while `FakeSherbrooke` is not a live quantum processor. The results therefore demonstrate the promotion workflow and its quality-gate behavior; they do not establish performance on a real QPU or over time-varying hardware conditions.
