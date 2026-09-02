#!/usr/bin/env python3
"""
QPromote — Declarative Progressive Delivery Pipeline for Quantum Circuits
Version: 1.1.0
Authors: Hassan Soubra, Pavan Kumar Naganaboina, Samuel Richard,
         Tuna Hacaloglu, Donatien Koulla Moulla, Pierre Bourque, Alain Abran

Usage:
    python qpromote.py run pipeline.yaml
"""
from __future__ import annotations

import argparse
import html
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml
from qiskit import QuantumCircuit, transpile
from qiskit_aer import AerSimulator
from qiskit_ibm_runtime.fake_provider import FakeManilaV2, FakeSherbrooke


# ── Constants ──────────────────────────────────────────────────────────────────

VERSION = "1.1.0"

# Known ideal distributions for supported circuits
IDEAL_DISTRIBUTIONS: Dict[str, Dict[str, float]] = {
    "bell":   {"00": 0.5,  "11": 0.5},
    "ghz":    {"000": 0.5, "111": 0.5},
    "grover": {"11": 1.0},          # 2-qubit Grover marks |11>
    "bv":     {"11": 1.0},          # BV with hidden string "11" outputs |11>
    "qft":    None,                  # QFT: compare Stage 2 vs Stage 1 baseline
}


# ── Utility ────────────────────────────────────────────────────────────────────

def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_counts(counts: Dict[str, int], n_bits: int) -> Dict[str, float]:
    """Normalize measurement counts to a probability distribution.
    
    Uses n_bits (number of CLASSICAL bits measured) to generate the state space.
    This correctly handles circuits where num_qubits != num_clbits (e.g. BV).
    """
    total = sum(counts.values())
    if total == 0:
        return {format(i, f'0{n_bits}b'): 0.0 for i in range(2 ** n_bits)}
    dist: Dict[str, float] = {}
    for bitstring, count in counts.items():
        # Strip spaces (Qiskit sometimes returns "0 1" format)
        key = str(bitstring).replace(" ", "")
        # Pad or truncate to n_bits
        if len(key) < n_bits:
            key = key.zfill(n_bits)
        elif len(key) > n_bits:
            key = key[-n_bits:]
        dist[key] = dist.get(key, 0.0) + int(count) / total
    return dist


def hellinger_fidelity(p: Dict[str, float], q: Dict[str, float]) -> float:
    """Compute Hellinger fidelity between two probability distributions.
    Returns a value in [0, 1] where 1.0 means identical distributions.
    Formula: H(P,Q) = (sum_x sqrt(P(x) * Q(x)))^2
    Reference: Hellinger (1909); García de la Barrera et al. (2023).
    """
    states = set(p) | set(q)
    score = sum(
        math.sqrt(max(p.get(s, 0.0), 0.0) * max(q.get(s, 0.0), 0.0))
        for s in states
    )
    return float(score ** 2)


def total_variation_distance(p: Dict[str, float], q: Dict[str, float]) -> float:
    """Compute Total Variation Distance between two distributions.
    Returns a value in [0, 1] where 0.0 means identical distributions.
    Formula: TVD(P,Q) = 0.5 * sum_x |P(x) - Q(x)|
    """
    states = set(p) | set(q)
    return 0.5 * sum(abs(p.get(s, 0.0) - q.get(s, 0.0)) for s in states)


def compute_cfp(circuit: QuantumCircuit) -> int:
    """Compute COSMIC Function Points using Gates' Occurrences approach.
    
    Each gate:        1 Entry + 1 Exit = 2 CFP
    Each measurement: 1 Write + 1 Read = 2 CFP
    Reference: Khattab, Elsayed & Soubra (2022); Soubra et al. (2025).
    """
    gate_cfp = sum(circuit.count_ops().values()) * 2
    measure_cfp = circuit.num_clbits * 2
    return gate_cfp + measure_cfp


# ── Circuit Definitions ────────────────────────────────────────────────────────

def bell_circuit() -> QuantumCircuit:
    """Bell state: 2 qubits, 2 classical bits. Expected: |00> + |11> (50/50)."""
    qc = QuantumCircuit(2, 2)
    qc.h(0); qc.cx(0, 1)
    qc.measure(0, 0); qc.measure(1, 1)
    return qc


def ghz_circuit() -> QuantumCircuit:
    """GHZ state: 3 qubits, 3 classical bits. Expected: |000> + |111> (50/50)."""
    qc = QuantumCircuit(3, 3)
    qc.h(0); qc.cx(0, 1); qc.cx(0, 2)
    qc.measure(0, 0); qc.measure(1, 1); qc.measure(2, 2)
    return qc


def grover_circuit() -> QuantumCircuit:
    """2-qubit Grover search for |11>. Expected: |11> with high probability."""
    qc = QuantumCircuit(2, 2)
    qc.h([0, 1])                    # superposition
    qc.cz(0, 1)                     # oracle: marks |11>
    qc.h([0, 1]); qc.x([0, 1])     # diffusion
    qc.cz(0, 1)
    qc.x([0, 1]); qc.h([0, 1])
    qc.measure(0, 0); qc.measure(1, 1)
    return qc


def bv_circuit() -> QuantumCircuit:
    """Bernstein-Vazirani with hidden string '11'.
    
    3 qubits (2 input + 1 ancilla), 2 classical bits.
    Measures only input qubits 0 and 1.
    Expected output: |11> with probability 1.0.
    
    Note: num_qubits=3 but num_clbits=2. normalize_counts uses num_clbits
    to generate the correct 2-bit state space for metric computation.
    """
    qc = QuantumCircuit(3, 2)
    qc.x(2); qc.h(2)               # ancilla in |-> state
    qc.h([0, 1])                    # Hadamard on input qubits
    qc.cx(0, 2); qc.cx(1, 2)       # oracle: s = "11" (both bits set)
    qc.h([0, 1])                    # Hadamard again
    qc.measure(0, 0); qc.measure(1, 1)
    return qc


def qft_circuit() -> QuantumCircuit:
    """3-qubit Quantum Fourier Transform applied to uniform superposition.
    
    Comparison strategy: relative fidelity between Stage 2 and Stage 1 baseline.
    No fixed ideal distribution — Stage 1 output serves as reference.
    """
    qc = QuantumCircuit(3, 3)
    # Uniform superposition input
    qc.h([0, 1, 2])
    # Manual 3-qubit QFT (avoids deprecated QFT class in Qiskit 2.x)
    qc.h(0)
    qc.cp(math.pi/2, 1, 0)
    qc.cp(math.pi/4, 2, 0)
    qc.h(1)
    qc.cp(math.pi/2, 2, 1)
    qc.h(2)
    # SWAP for bit-reversal
    qc.swap(0, 2)
    qc.measure(0, 0); qc.measure(1, 1); qc.measure(2, 2)
    return qc


CIRCUIT_REGISTRY: Dict[str, Any] = {
    "bell":   bell_circuit,
    "ghz":    ghz_circuit,
    "grover": grover_circuit,
    "bv":     bv_circuit,
    "qft":    qft_circuit,
}


# ── Backend Resolution ─────────────────────────────────────────────────────────

def resolve_backend(name: str) -> Tuple[str, Any]:
    key = name.strip().lower().replace("-", "_")
    if key in {"aer", "aer_simulator", "simulator"}:
        return "AerSimulator", AerSimulator()
    if key in {"fakemanilav2", "manila", "fake_manila_v2"}:
        return "FakeManilaV2", FakeManilaV2()
    if key in {"fakesherbrooke", "sherbrooke", "fake_sherbrooke"}:
        return "FakeSherbrooke", FakeSherbrooke()
    raise ValueError(f"Unsupported backend '{name}'. "
                     f"Supported: aer_simulator, FakeManilaV2, FakeSherbrooke")


# ── Database ───────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT    NOT NULL,
    circuit_name     TEXT    NOT NULL,
    stage_name       TEXT    NOT NULL,
    backend_name     TEXT    NOT NULL,
    shots            INTEGER NOT NULL,
    hellinger        REAL,
    tvd              REAL,
    fidelity         REAL,
    gate_count       INTEGER,
    qubit_count      INTEGER,
    classical_bits   INTEGER,
    cfp              INTEGER,
    decision         TEXT    NOT NULL,
    notes            TEXT
);
"""

def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def store_evidence(conn: sqlite3.Connection, rec: Dict[str, Any]) -> None:
    conn.execute("""
        INSERT INTO evidence
            (timestamp, circuit_name, stage_name, backend_name, shots,
             hellinger, tvd, fidelity, gate_count, qubit_count,
             classical_bits, cfp, decision, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        rec["timestamp"], rec["circuit_name"], rec["stage_name"],
        rec["backend_name"], rec["shots"],
        rec.get("hellinger"), rec.get("tvd"), rec.get("fidelity"),
        rec.get("gate_count"), rec.get("qubit_count"),
        rec.get("classical_bits"), rec.get("cfp"),
        rec["decision"], rec.get("notes"),
    ))
    conn.commit()


def write_html_report(records: List[Dict[str, Any]], path: Path) -> None:
    """Write a standalone HTML report suitable for a GitHub Actions artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for rec in records:
        rows.append(
            "<tr>"
            f"<td>{html.escape(rec['circuit_name'].upper())}</td>"
            f"<td>{html.escape(rec['stage_name'])}</td>"
            f"<td>{html.escape(rec['backend_name'])}</td>"
            f"<td>{rec['hellinger']:.4f}</td>"
            f"<td>{rec['tvd']:.4f}</td>"
            f"<td>{rec['cfp']}</td>"
            f"<td class=\"{rec['decision'].lower()}\">{html.escape(rec['decision'])}</td>"
            "</tr>"
        )

    chart_groups: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        chart_groups.setdefault(rec["circuit_name"], []).append(rec)
    charts = []
    for circuit_name, circuit_records in chart_groups.items():
        bars = []
        for index, rec in enumerate(circuit_records):
            x = 90 + index * 170
            height = max(2, round(rec["hellinger"] * 190))
            y = 220 - height
            color = {"PASS": "#16803c", "BLOCK": "#c33b30", "DEMONSTRATION": "#1769aa"}.get(rec["decision"], "#59636e")
            label = html.escape(rec["stage_name"].split(" ")[1])
            bars.append(
                f'<rect x="{x}" y="{y}" width="86" height="{height}" fill="{color}" rx="4" />'
                f'<text x="{x + 43}" y="{y - 8}" text-anchor="middle">{rec["hellinger"]:.3f}</text>'
                f'<text x="{x + 43}" y="245" text-anchor="middle">{label}</text>'
            )
        charts.append(
            f'<section><h2>{html.escape(circuit_name.upper())}</h2>'
            '<svg viewBox="0 0 620 270" role="img" aria-label="Hellinger fidelity by stage">'
            '<line x1="55" y1="220" x2="570" y2="220" stroke="#8b98a5" />'
            '<line x1="55" y1="30" x2="55" y2="220" stroke="#8b98a5" />'
            '<line x1="55" y1="49" x2="570" y2="49" stroke="#c33b30" stroke-dasharray="6 5" />'
            '<text x="8" y="54">0.90</text><text x="20" y="225">0</text>'
            + "".join(bars)
            + '</svg><p class="legend">Dashed line: Stage 2 Hellinger threshold 0.90. Green = pass, red = block, blue = demonstration.</p></section>'
        )

    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QPromote v{VERSION} results</title><style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:1200px;margin:0 auto;padding:32px;color:#17212b;background:#f4f6f8}}
header,section{{background:#fff;border:1px solid #d8e0e7;border-radius:8px;padding:22px;margin:0 0 20px;box-shadow:0 2px 8px #17212b12}}
h1{{margin:0 0 6px;color:#123b5d}} h2{{margin-top:0;color:#123b5d}} .summary{{display:flex;gap:24px;flex-wrap:wrap}}
.summary strong{{font-size:1.8rem;display:block}} table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:10px;border-bottom:1px solid #e3e8ed;text-align:left}}
th{{background:#eaf0f4}} .pass{{color:#16803c;font-weight:700}} .block{{color:#c33b30;font-weight:700}} .demonstration{{color:#1769aa;font-weight:700}}
svg{{width:100%;max-width:620px;height:auto;background:#fbfcfd;border:1px solid #e3e8ed}} svg text{{font:14px system-ui;fill:#17212b}} .legend{{color:#59636e;font-size:.9rem}}
</style></head><body><header><h1>QPromote v{VERSION} results</h1>
<p>Generated by the local simulator pipeline. Stage 3 uses a fake hardware proxy.</p>
<div class="summary"><div><strong>{len(records)}</strong> stage records</div><div><strong>{sum(rec['decision'] == 'PASS' for rec in records)}</strong> passed</div><div><strong>{sum(rec['decision'] == 'BLOCK' for rec in records)}</strong> blocked</div><div><strong>{len(chart_groups)}</strong> circuits</div></div></header>
{"".join(charts)}<section><h2>Evidence table</h2><table><thead><tr><th>Circuit</th><th>Stage</th><th>Backend</th><th>Hellinger</th><th>TVD</th><th>CFP</th><th>Decision</th></tr></thead><tbody>{"".join(rows)}</tbody></table></section></body></html>"""
    path.write_text(document, encoding="utf-8")


# ── Pipeline Execution ─────────────────────────────────────────────────────────

def run_stage(
    circuit_name: str,
    stage_name: str,
    backend_name: str,
    circuit: QuantumCircuit,
    thresholds: Dict[str, float],
    ideal_dist: Dict[str, float] | None,
    shots: int = 4096,
) -> Dict[str, Any]:
    """Execute one pipeline stage and evaluate quality gate."""
    backend_label, backend = resolve_backend(backend_name)

    # Transpile and execute
    t_qc = transpile(circuit, backend, optimization_level=1)
    counts = backend.run(t_qc, shots=shots).result().get_counts()

    # Use num_clbits for normalization — critical fix for BV (3q/2c mismatch)
    n_bits = circuit.num_clbits
    measured = normalize_counts(counts, n_bits)

    # Metrics
    ref = ideal_dist if ideal_dist is not None else measured  # QFT: self-compare at Stage 1
    h_score  = hellinger_fidelity(measured, ref)
    tvd_val  = total_variation_distance(measured, ref)
    fidelity = max(0.0, 1.0 - tvd_val)
    gc       = sum(circuit.count_ops().values())
    cfp_val  = compute_cfp(circuit)

    # Gate decision
    stage_key = stage_name.lower().replace(" ", "")
    if "stage3" in stage_key or backend_label == "FakeSherbrooke":
        decision = "DEMONSTRATION"
        notes = "Hardware proxy stage. FakeSherbrooke used as QPU demonstration."
    elif "stage2" in stage_key:
        min_h   = float(thresholds.get("hellinger_fidelity", 0.90))
        max_tvd = float(thresholds.get("tvd", 0.10))
        if h_score >= min_h and tvd_val <= max_tvd:
            decision = "PASS"
        else:
            decision = "BLOCK"
        notes = (f"Noisy gate: H>={min_h:.2f}, TVD<={max_tvd:.2f}. "
                 f"Got H={h_score:.4f}, TVD={tvd_val:.4f}.")
    else:  # stage1
        min_fid = float(thresholds.get("fidelity", 0.90))
        decision = "PASS" if fidelity >= min_fid else "BLOCK"
        notes = f"Ideal gate: fidelity>={min_fid:.2f}. Got {fidelity:.4f}."

    return {
        "timestamp":      utc_timestamp(),
        "circuit_name":   circuit_name,
        "stage_name":     stage_name,
        "backend_name":   backend_label,
        "shots":          shots,
        "hellinger":      round(h_score, 6),
        "tvd":            round(tvd_val, 6),
        "fidelity":       round(fidelity, 6),
        "gate_count":     gc,
        "qubit_count":    circuit.num_qubits,
        "classical_bits": circuit.num_clbits,
        "cfp":            cfp_val,
        "decision":       decision,
        "notes":          notes,
        "_measured_dist": measured,   # internal — for QFT Stage 2 baseline
    }


def run_pipeline(pipeline_path: Path) -> List[Dict[str, Any]]:
    """Load YAML config and execute the full pipeline."""
    if not pipeline_path.exists():
        raise FileNotFoundError(f"Pipeline file not found: {pipeline_path}")
    with pipeline_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    db_path = Path(cfg.get("db_path", "qpromote_evidence.db"))
    conn    = init_db(db_path)
    shots   = int(cfg.get("shots", 4096))
    halt_on_block = bool(cfg.get("halt_on_block", False))
    report_path = Path(cfg.get("report_path", "qpromote_report.html"))
    records: List[Dict[str, Any]] = []

    for stage_cfg in cfg.get("stages", []):
        stage_name   = str(stage_cfg.get("name", "stage"))
        backend_name = str(stage_cfg.get("backend", "aer_simulator"))
        circuit_name = str(stage_cfg.get("circuit", "bell")).lower()
        thresholds   = stage_cfg.get("thresholds", {}) or {}

        if circuit_name not in CIRCUIT_REGISTRY:
            print(f"  [SKIP] Unknown circuit '{circuit_name}' in {stage_name}")
            continue

        circuit    = CIRCUIT_REGISTRY[circuit_name]()
        ideal_dist = IDEAL_DISTRIBUTIONS.get(circuit_name)

        # QFT: Stage 2 and Stage 3 compare against Stage 1 output, not a fixed ideal
        if ideal_dist is None and records:
            prev_stage1 = next(
                (r for r in records
                 if r["circuit_name"] == circuit_name and "stage1" in r["stage_name"].lower()),
                None
            )
            if prev_stage1:
                ideal_dist = prev_stage1.get("_measured_dist")

        rec = run_stage(
            circuit_name=circuit_name,
            stage_name=stage_name,
            backend_name=backend_name,
            circuit=circuit,
            thresholds=thresholds,
            ideal_dist=ideal_dist,
            shots=shots,
        )

        # Persist (drop internal key before storage)
        storage_rec = {k: v for k, v in rec.items() if not k.startswith("_")}
        store_evidence(conn, storage_rec)
        records.append(rec)

        flag = "✓" if rec["decision"] in ("PASS", "DEMONSTRATION") else "✗"
        print(f"  {flag} {stage_name} | {rec['backend_name']:<18} | "
              f"H={rec['hellinger']:.4f}  TVD={rec['tvd']:.4f}  "
              f"CFP={rec['cfp']:<4} → {rec['decision']}")

        # Halt pipeline on first BLOCK in Stage 2+ (after Stage 1 baseline established)
        # Note: For demo/testing, disable halt by commenting the block below
        # if rec["decision"] == "BLOCK" and "stage2" in stage_name.lower():
        #     print(f"\n  ⛔ Pipeline halted at {stage_name}: {rec['notes']}")
        #     break
        # Halt pipeline only when explicitly configured for production mode.
        if rec["decision"] == "BLOCK" and halt_on_block:
            print(f"\n  ⛔ Pipeline halted at {stage_name}: {rec['notes']}")
            break

    conn.close()
    write_html_report(records, report_path)
    print(f"Report written to: {report_path}")
    return records


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"QPromote v{VERSION} — Quantum Progressive Delivery Pipeline"
    )
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run", help="Execute a pipeline")
    run_p.add_argument("pipeline", nargs="?", default="pipeline.yaml",
                       help="Path to YAML pipeline file")
    args = parser.parse_args(argv)

    if args.command == "run":
        print(f"\nQPromote v{VERSION} — starting pipeline: {args.pipeline}\n")
        try:
            records = run_pipeline(Path(args.pipeline))
            db = Path(records[0].get("_db", "qpromote_evidence.db")) if records else Path("qpromote_evidence.db")
            print(f"\nDone. Evidence stored in: qpromote_evidence.db")
            return 0
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
