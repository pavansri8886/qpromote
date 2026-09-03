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
- qiskit-aer
- qiskit-ibm-runtime
- PyYAML

## Evidence Database

SQLite evidence bundle records: timestamp, circuit, stage, backend,
Hellinger fidelity, TVD, fidelity, gate count, qubit count, CFP, decision.

```
sqlite3 qpromote_evidence.db "SELECT circuit_name, stage_name, hellinger, tvd, decision FROM evidence;"
```

## Citation

If using this code, please cite the associated thesis:
P. K. Naganaboina, "QPromote: A Declarative Progressive Delivery Pipeline
for Simulator-to-Hardware Quantum Circuit Promotion," MSc Thesis,
ECE Paris / ÉTS Montréal, September 2026.
