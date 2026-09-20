# QPromote

A declarative progressive delivery pipeline for quantum circuit promotion from 
ideal simulation to noisy simulation to hardware execution.

**Authors:** Hassan Soubra, Pavan Kumar Naganaboina, Samuel Richard, 
Tuna Hacaloglu, Donatien Koulla Moulla, Pierre Bourque, Alain Abran

## Quick Start

```bash
python -m pip install -r requirements.txt
python qpromote.py run pipeline.yaml
```

For statistical Stage 2 evidence, run 20 repetitions per circuit:

```bash
python qpromote.py repeated-run pipeline.yaml --runs 20
```

To evaluate the Stage 2 threshold policy:

```bash
python qpromote.py threshold-scan pipeline.yaml --thresholds 0.80,0.85,0.90,0.925,0.95
```

The run also creates `qpromote_report.html`, a standalone visual report with
Hellinger charts and the complete evidence table. In GitHub Actions, download
the `qpromote-results` artifact to view it locally.

Stage 3 uses `FakeSherbrooke` as a hardware proxy. A circuit blocked at Stage 2
is not executed at Stage 3; the pipeline records that stage as `SKIPPED` so the
15-stage audit trail remains complete without treating a proxy run as promotion.

## Supported Circuits

- Bell state (2 qubits)
- GHZ state (3 qubits)
- Grover search (2 qubits)
- Bernstein-Vazirani (3 qubits, 2 classical bits)
- Quantum Fourier Transform (3 qubits)

## Pipeline Stages

Stage
Backend
Gate Metric

Stage 1
AerSimulator (statevector)
Fidelity ≥ 0.90

Stage 2
FakeManilaV2 (noise model)
Hellinger ≥ 0.90, TVD ≤ 0.10

Stage 3
FakeSherbrooke (hardware proxy)
Demonstration only

## Environment

- Python 3.12
- qiskit==2.5.2
- qiskit-aer==0.17.2
- qiskit-ibm-runtime==0.49.0
- PyYAML==6.0.2

## Evidence Database

SQLite evidence bundle records include run ID, timestamp, circuit, stage,
backend, shots, Hellinger fidelity, TVD, fidelity, gate count, qubit count,
CFP, Aer version, IBM Runtime version, and decision. Repeated Stage 2 results
are stored in the same `evidence` table. Threshold experiments are stored in
the separate `threshold_scan` table.

```
sqlite3 qpromote_evidence.db "SELECT circuit_name, stage_name, hellinger, tvd, decision FROM evidence;"
```

Example repeated-run summary:

```bash
sqlite3 qpromote_evidence.db "SELECT circuit_name, AVG(hellinger), COUNT(*), SUM(decision = 'PASS') FROM evidence WHERE stage_name LIKE '%repeat_%' GROUP BY circuit_name;"
```

## Citation

If using this code, please cite the associated thesis:
P. K. Naganaboina, "QPromote: A Declarative Progressive Delivery Pipeline
for Simulator-to-Hardware Quantum Circuit Promotion," MSc Thesis,
ECE Paris / ÉTS Montréal, September 2026.
