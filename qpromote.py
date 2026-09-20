#!/usr/bin/env python3
"""
QPromote — Declarative Progressive Delivery Pipeline for Quantum Circuits
Version: 1.4.0
Authors: Hassan Soubra, Pavan Kumar Naganaboina, Samuel Richard,
         Tuna Hacaloglu, Donatien Koulla Moulla, Pierre Bourque, Alain Abran

Usage:
    python qpromote.py run pipeline.yaml
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import subprocess
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml
import qiskit_aer
import qiskit_ibm_runtime
from qiskit import QuantumCircuit, transpile
from qiskit_aer import AerSimulator
from qiskit_ibm_runtime.fake_provider import FakeManilaV2, FakeSherbrooke


# ── Constants ──────────────────────────────────────────────────────────────────

VERSION = "1.4.0"
SUPPORTED_BACKENDS = {
    "ideal": "aer_simulator",
    "noisy": "fakemanilav2",
    "proxy": "fakesherbrooke",
}

# Known ideal distributions for supported circuits
IDEAL_DISTRIBUTIONS: Dict[str, Dict[str, float]] = {
    "bell":   {"00": 0.5,  "11": 0.5},
    "ghz":    {"000": 0.5, "111": 0.5},
    "grover": {"11": 1.0},          # 2-qubit Grover marks |11>
    "bv":     {"11": 1.0},          # BV with hidden string "11" outputs |11>
    "qft":    {"000": 1.0},         # QFT(|+++>) = |000>
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


def recalculate_metrics(raw_counts: Dict[str, int], reference: Dict[str, float], n_bits: int) -> Tuple[float, float, float]:
    measured = normalize_counts(raw_counts, n_bits)
    tvd = total_variation_distance(measured, reference)
    return hellinger_fidelity(measured, reference), tvd, max(0.0, 1.0 - tvd)


def compute_cfp(circuit: QuantumCircuit) -> int:
    """Compute COSMIC Function Points using Gates' Occurrences approach.
    
    Each gate:        1 Entry + 1 Exit = 2 CFP
    Each measurement: 1 Write + 1 Read = 2 CFP
    Reference: Khattab, Elsayed & Soubra (2022); Soubra et al. (2025).
    """
    gate_cfp = count_quantum_operations(circuit) * 2
    measure_cfp = count_measurements(circuit) * 2
    return gate_cfp + measure_cfp


def count_measurements(circuit: QuantumCircuit) -> int:
    return int(circuit.count_ops().get("measure", 0))


def count_quantum_operations(circuit: QuantumCircuit) -> int:
    """Count operations other than classical measurements and barriers."""
    return sum(
        count for name, count in circuit.count_ops().items()
        if name not in {"measure", "barrier"}
    )


def make_seed(base_seed: int, *parts: object) -> int:
    payload = ":".join([str(base_seed), *(str(part) for part in parts)])
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8], 16)


def git_commit() -> str:
    repo_dir = Path(__file__).resolve().parent
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def source_provenance() -> Dict[str, Any]:
    source_path = Path(__file__).resolve()
    repo_dir = source_path.parent
    try:
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain", "--", str(source_path)],
            cwd=repo_dir, check=True, capture_output=True, text=True,
        ).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        dirty = None
    return {
        "code_commit": git_commit(),
        "working_tree_dirty": dirty,
        "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
    }


def config_identity(cfg: Dict[str, Any]) -> str:
    payload = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def validate_positive_int(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def canonical_backend(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def validate_scan_thresholds(values: List[str]) -> List[float]:
    parsed: List[float] = []
    for raw in values:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid scan threshold '{raw}': expected a finite number") from exc
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"scan threshold '{raw}' must be finite and between 0 and 1")
        if value in parsed:
            raise ValueError(f"duplicate scan threshold '{raw}'")
        parsed.append(value)
    if not parsed:
        raise ValueError("at least one scan threshold is required")
    return parsed


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

    The expected measured output is |000> with probability 1.
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
        return "AerSimulator", AerSimulator(method="statevector")
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
    run_id           TEXT    NOT NULL,
    timestamp        TEXT    NOT NULL,
    circuit_name     TEXT    NOT NULL,
    stage_name       TEXT    NOT NULL,
    stage_type       TEXT    NOT NULL,
    backend_name     TEXT    NOT NULL,
    shots            INTEGER NOT NULL,
    requested_shots  INTEGER NOT NULL,
    executed_shots   INTEGER NOT NULL,
    hellinger        REAL,
    tvd              REAL,
    distribution_similarity REAL,
    gate_count       INTEGER,
    qubit_count      INTEGER,
    classical_bits   INTEGER,
    cfp              INTEGER,
    aer_version      TEXT    NOT NULL,
    runtime_version  TEXT    NOT NULL,
    seed_simulator   INTEGER,
    seed_transpiler  INTEGER,
    raw_counts       TEXT,
    reference_distribution TEXT,
    code_commit      TEXT,
    working_tree_dirty INTEGER,
    source_sha256    TEXT,
    config_identity  TEXT,
    decision         TEXT    NOT NULL,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS threshold_scan (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT    NOT NULL,
    timestamp        TEXT    NOT NULL,
    circuit_name     TEXT    NOT NULL,
    repetition       INTEGER NOT NULL,
    threshold        REAL    NOT NULL,
    shots            INTEGER NOT NULL,
    requested_shots  INTEGER NOT NULL,
    executed_shots   INTEGER NOT NULL,
    hellinger        REAL,
    tvd              REAL,
    decision         TEXT    NOT NULL,
    aer_version      TEXT    NOT NULL,
    runtime_version  TEXT    NOT NULL,
    seed_simulator  INTEGER,
    seed_transpiler INTEGER,
    raw_counts      TEXT,
    reference_distribution TEXT,
    code_commit     TEXT,
    config_identity TEXT,
    execution_id INTEGER
);

CREATE TABLE IF NOT EXISTS scan_executions (
    execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    circuit_name TEXT NOT NULL,
    repetition INTEGER NOT NULL,
    backend_name TEXT NOT NULL,
    requested_shots INTEGER NOT NULL,
    executed_shots INTEGER NOT NULL,
    hellinger REAL NOT NULL,
    tvd REAL NOT NULL,
    raw_counts TEXT NOT NULL,
    reference_distribution TEXT NOT NULL,
    seed_simulator INTEGER NOT NULL,
    seed_transpiler INTEGER NOT NULL,
    aer_version TEXT NOT NULL,
    runtime_version TEXT NOT NULL,
    code_commit TEXT NOT NULL,
    working_tree_dirty INTEGER,
    source_sha256 TEXT NOT NULL,
    config_identity TEXT NOT NULL
);
"""


def new_run_id() -> str:
    return str(uuid.uuid4())[:8]


def dependency_versions() -> Tuple[str, str]:
    return qiskit_aer.__version__, qiskit_ibm_runtime.__version__

def init_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence)")}
    if "run_id" not in columns:
        conn.execute("ALTER TABLE evidence ADD COLUMN run_id TEXT NOT NULL DEFAULT 'legacy'")
    if "aer_version" not in columns:
        conn.execute("ALTER TABLE evidence ADD COLUMN aer_version TEXT NOT NULL DEFAULT 'unknown'")
    if "runtime_version" not in columns:
        conn.execute("ALTER TABLE evidence ADD COLUMN runtime_version TEXT NOT NULL DEFAULT 'unknown'")
    migrations = {
        "stage_type": "TEXT NOT NULL DEFAULT 'unknown'",
        "requested_shots": "INTEGER NOT NULL DEFAULT 0",
        "executed_shots": "INTEGER NOT NULL DEFAULT 0",
        "distribution_similarity": "REAL",
        "seed_simulator": "INTEGER",
        "seed_transpiler": "INTEGER",
        "raw_counts": "TEXT",
        "reference_distribution": "TEXT",
        "code_commit": "TEXT",
        "working_tree_dirty": "INTEGER",
        "source_sha256": "TEXT",
        "config_identity": "TEXT",
        "execution_id": "INTEGER",
    }
    for column, definition in migrations.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE evidence ADD COLUMN {column} {definition}")
    threshold_columns = {row[1] for row in conn.execute("PRAGMA table_info(threshold_scan)")}
    threshold_migrations = {
        "repetition": "INTEGER NOT NULL DEFAULT 0",
        "requested_shots": "INTEGER NOT NULL DEFAULT 0",
        "executed_shots": "INTEGER NOT NULL DEFAULT 0",
        "seed_simulator": "INTEGER",
        "seed_transpiler": "INTEGER",
        "raw_counts": "TEXT",
        "reference_distribution": "TEXT",
        "code_commit": "TEXT",
        "config_identity": "TEXT",
        "execution_id": "INTEGER",
    }
    for column, definition in threshold_migrations.items():
        if column not in threshold_columns:
            conn.execute(f"ALTER TABLE threshold_scan ADD COLUMN {column} {definition}")
    conn.commit()
    return conn


def store_scan_execution(conn: sqlite3.Connection, rec: Dict[str, Any], repetition: int) -> int:
    provenance = source_provenance()
    cursor = conn.execute("""
        INSERT INTO scan_executions
            (run_id, circuit_name, repetition, backend_name, requested_shots,
             executed_shots, hellinger, tvd, raw_counts, reference_distribution,
             seed_simulator, seed_transpiler, aer_version, runtime_version,
             code_commit, working_tree_dirty, source_sha256, config_identity)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        rec["run_id"], rec["circuit_name"], repetition, rec["backend_name"],
        rec["requested_shots"], rec["executed_shots"], rec["hellinger"], rec["tvd"],
        json.dumps(rec["raw_counts"], sort_keys=True),
        json.dumps(rec["reference_distribution"], sort_keys=True),
        rec["seed_simulator"], rec["seed_transpiler"], rec["aer_version"],
        rec["runtime_version"], provenance["code_commit"],
        None if provenance["working_tree_dirty"] is None else int(provenance["working_tree_dirty"]),
        provenance["source_sha256"], rec["config_identity"],
    ))
    conn.commit()
    return int(cursor.lastrowid)


def store_evidence(conn: sqlite3.Connection, rec: Dict[str, Any]) -> None:
    conn.execute("""
        INSERT INTO evidence
            (run_id, timestamp, circuit_name, stage_name, stage_type, backend_name, shots,
             requested_shots, executed_shots, hellinger, tvd,
             distribution_similarity, gate_count, qubit_count, classical_bits,
             cfp, aer_version, runtime_version, seed_simulator, seed_transpiler,
               raw_counts, reference_distribution, code_commit, working_tree_dirty,
               source_sha256, config_identity,
             decision, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        rec["run_id"], rec["timestamp"], rec["circuit_name"], rec["stage_name"],
        rec["stage_type"],
        rec["backend_name"], rec["shots"],
        rec["requested_shots"], rec["executed_shots"],
        rec.get("hellinger"), rec.get("tvd"), rec.get("distribution_similarity"),
        rec.get("gate_count"), rec.get("qubit_count"),
        rec.get("classical_bits"), rec.get("cfp"),
        rec["aer_version"], rec["runtime_version"],
        rec.get("seed_simulator"), rec.get("seed_transpiler"),
        json.dumps(rec.get("raw_counts", {}), sort_keys=True),
        json.dumps(rec.get("reference_distribution", {}), sort_keys=True),
        rec.get("code_commit"), rec.get("working_tree_dirty"), rec.get("source_sha256"),
        rec.get("config_identity"),
        rec["decision"], rec.get("notes"),
    ))
    conn.commit()


def store_threshold_result(
    conn: sqlite3.Connection,
    rec: Dict[str, Any],
    threshold: float,
    repetition: int,
    execution_id: int | None = None,
) -> None:
    conn.execute("""
        INSERT INTO threshold_scan
            (run_id, timestamp, circuit_name, repetition, threshold, shots,
               requested_shots, executed_shots, hellinger, tvd, decision,
               aer_version, runtime_version, seed_simulator, seed_transpiler,
               raw_counts, reference_distribution, code_commit, config_identity, execution_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        rec["run_id"], rec["timestamp"], rec["circuit_name"], repetition, threshold,
        0, 0, 0,
        rec.get("hellinger"), rec.get("tvd"), rec["decision"],
        rec["aer_version"], rec["runtime_version"], rec.get("seed_simulator"),
        None, None, None, None, None, execution_id,
    ))
    conn.commit()


def write_html_report(records: List[Dict[str, Any]], path: Path) -> None:
    """Write a standalone visual report for local use and Actions artifacts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for record in records:
        decision = record["decision"]
        hellinger = "-" if record["hellinger"] is None else f"{record['hellinger']:.4f}"
        tvd = "-" if record["tvd"] is None else f"{record['tvd']:.4f}"
        rows.append(
            f"<tr><td>{html.escape(record['circuit_name'].upper())}</td>"
            f"<td>{html.escape(record['stage_name'])}</td>"
            f"<td>{html.escape(record['backend_name'])}</td>"
            f"<td>{hellinger}</td><td>{tvd}</td>"
            f"<td class='{decision.lower()}'>{html.escape(decision)}</td></tr>"
        )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QPromote v{VERSION} results</title><style>
body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#17212b;background:#f4f6f8}}
section{{background:white;border:1px solid #d8e0e7;border-radius:8px;padding:1.25rem;margin-bottom:1.25rem}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:.6rem;border-bottom:1px solid #e3e8ed;text-align:left}}th{{background:#eaf0f4}}
.pass{{color:#16803c;font-weight:700}}.block{{color:#c33b30;font-weight:700}}.demonstration{{color:#1769aa;font-weight:700}}.skipped{{color:#7a5b00;font-weight:700}}
</style></head><body><section><h1>QPromote v{VERSION} results</h1>
<p>{len(records)} stage records from the local simulator pipeline.</p></section>
<section><h2>Evidence table</h2><table><thead><tr><th>Circuit</th><th>Stage</th><th>Backend</th><th>Hellinger</th><th>TVD</th><th>Decision</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></section></body></html>"""
    path.write_text(document, encoding="utf-8")


def _write_csv(path: Path, headers: List[str], rows: List[Tuple[Any, ...]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def _svg_bar_chart(path: Path, title: str, labels: List[str], values: List[float], maximum: float = 1.0) -> None:
    width, height = 900, 420
    margin = 60
    plot_width = width - 2 * margin
    bar_width = plot_width / max(len(values), 1) * 0.68
    scale = (height - 2 * margin) / maximum
    bars = []
    for index, (label, value) in enumerate(zip(labels, values)):
        x = margin + index * plot_width / max(len(values), 1) + bar_width * 0.24
        y = height - margin - value * scale
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{value * scale:.1f}" fill="#1769aa"/>'
            f'<text x="{x + bar_width / 2:.1f}" y="{height - 25}" text-anchor="middle" font-size="13">{html.escape(label)}</text>'
            f'<text x="{x + bar_width / 2:.1f}" y="{max(y - 6, 15):.1f}" text-anchor="middle" font-size="12">{value:.3f}</text>'
        )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="white"/><text x="{width/2}" y="28" text-anchor="middle" font-size="18" font-weight="bold">{html.escape(title)}</text>
<line x1="{margin}" y1="{height-margin}" x2="{width-margin}" y2="{height-margin}" stroke="#333"/>
{''.join(bars)}</svg>'''
    path.write_text(svg, encoding="utf-8")


def generate_reports(
    db_path: Path,
    output_dir: Path,
    pipeline_run: str,
    repeated_run: str,
    threshold_run: str,
) -> Dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    pipeline_rows = conn.execute(
        "SELECT * FROM evidence WHERE run_id=? ORDER BY id", (pipeline_run,)
    ).fetchall()
    repeated_rows = conn.execute(
        "SELECT * FROM evidence WHERE run_id=? ORDER BY circuit_name, id", (repeated_run,)
    ).fetchall()
    threshold_rows = conn.execute(
        "SELECT * FROM threshold_scan WHERE run_id=? ORDER BY circuit_name, repetition, threshold",
        (threshold_run,),
    ).fetchall()
    execution_rows = conn.execute(
        "SELECT * FROM scan_executions WHERE run_id=? ORDER BY circuit_name, repetition",
        (threshold_run,),
    ).fetchall()
    conn.close()
    missing = []
    if not pipeline_rows: missing.append(f"pipeline run {pipeline_run}")
    if not repeated_rows: missing.append(f"repeated run {repeated_run}")
    if not threshold_rows: missing.append(f"threshold run {threshold_run}")
    if not execution_rows: missing.append(f"scan executions for {threshold_run}")
    if missing:
        raise ValueError("missing selected experiment data: " + ", ".join(missing))
    output_dir.mkdir(parents=True, exist_ok=True)

    pipeline_headers = ["run_id", "circuit_name", "stage_name", "stage_type", "backend_name", "requested_shots", "executed_shots", "hellinger", "tvd", "distribution_similarity", "cfp", "decision"]
    _write_csv(output_dir / "pipeline.csv", pipeline_headers, [tuple(row[h] for h in pipeline_headers) for row in pipeline_rows])

    repeated_headers = ["circuit_name", "sample_size", "mean_hellinger", "sample_sd_hellinger", "pass_count", "pass_rate"]
    repeated_summary = []
    for circuit in sorted({row["circuit_name"] for row in repeated_rows}):
        values = [row["hellinger"] for row in repeated_rows if row["circuit_name"] == circuit]
        passes = sum(row["decision"] == "PASS" for row in repeated_rows if row["circuit_name"] == circuit)
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1) if len(values) > 1 else 0.0
        repeated_summary.append((circuit, len(values), f"{mean:.6f}", f"{math.sqrt(variance):.6f}", passes, f"{passes/len(values):.4f}"))
    _write_csv(output_dir / "repeated_summary.csv", repeated_headers, repeated_summary)

    threshold_headers = ["circuit_name", "repetition", "threshold", "hellinger", "tvd", "decision", "execution_id"]
    _write_csv(output_dir / "threshold_decisions.csv", threshold_headers, [tuple(row[h] for h in threshold_headers) for row in threshold_rows])
    execution_headers = ["execution_id", "run_id", "circuit_name", "repetition", "backend_name", "requested_shots", "executed_shots", "hellinger", "tvd", "seed_simulator", "seed_transpiler", "code_commit", "working_tree_dirty", "source_sha256", "config_identity"]
    _write_csv(output_dir / "scan_executions.csv", execution_headers, [tuple(row[h] for h in execution_headers) for row in execution_rows])

    threshold_summary = []
    for circuit in sorted({row["circuit_name"] for row in threshold_rows}):
        for threshold in sorted({row["threshold"] for row in threshold_rows if row["circuit_name"] == circuit}):
            subset = [row for row in threshold_rows if row["circuit_name"] == circuit and row["threshold"] == threshold]
            passes = sum(row["decision"] == "PASS" for row in subset)
            threshold_summary.append((circuit, threshold, len(subset), passes, f"{passes/len(subset):.4f}"))
    _write_csv(output_dir / "threshold_summary.csv", ["circuit_name", "threshold", "sample_size", "pass_count", "pass_rate"], threshold_summary)
    _svg_bar_chart(output_dir / "repeated_mean_hellinger.svg", "Repeated Stage 2 mean Hellinger", [row[0] for row in repeated_summary], [float(row[2]) for row in repeated_summary])
    _svg_bar_chart(output_dir / "repeated_pass_rate.svg", "Repeated Stage 2 pass rate", [row[0] for row in repeated_summary], [float(row[5]) for row in repeated_summary])
    _svg_bar_chart(
        output_dir / "threshold_pass_rate.svg", "Threshold pass rates",
        [f"{row[0]}@{row[1]:g}" for row in threshold_summary],
        [float(row[4]) for row in threshold_summary],
    )

    provenance_sources = pipeline_rows + repeated_rows + execution_rows
    provenance_sources = [row for row in provenance_sources if "code_commit" in row.keys()]
    manifest = {
        "database": str(db_path),
        "pipeline_run_id": pipeline_run,
        "repeated_run_id": repeated_run,
        "threshold_run_id": threshold_run,
        "pipeline_records": len(pipeline_rows),
        "repeated_records": len(repeated_rows),
        "scan_executions": len(execution_rows),
        "threshold_decisions": len(threshold_rows),
        "scan_executed_shots": sum(row["executed_shots"] for row in execution_rows),
        "provenance": sorted({(row["code_commit"], row["config_identity"], row["working_tree_dirty"], row["source_sha256"]) for row in provenance_sources if "code_commit" in row.keys()}),
        "aer_versions": sorted({row["aer_version"] for row in provenance_sources if "aer_version" in row.keys()}),
        "runtime_versions": sorted({row["runtime_version"] for row in provenance_sources if "runtime_version" in row.keys()}),
    }
    (output_dir / "provenance.json").write_text(json.dumps(manifest, indent=2, default=list), encoding="utf-8")
    summary_lines = [
        "# QPromote Selected-Run Summary", "",
        f"- Database: `{db_path}`", f"- Pipeline run: `{pipeline_run}`",
        f"- Repeated run: `{repeated_run}`", f"- Threshold run: `{threshold_run}`",
        f"- Pipeline records: {len(pipeline_rows)}", f"- Repeated records: {len(repeated_rows)}",
        f"- Actual scan executions: {len(execution_rows)}",
        f"- Threshold decisions: {len(threshold_rows)}",
        f"- Actual scan shots: {manifest['scan_executed_shots']}", "",
        "## Pipeline", "", "| Circuit | Stage | Decision | Hellinger | TVD | Executed shots |", "|---|---|---|---:|---:|---:|",
    ]
    for row in pipeline_rows:
        summary_lines.append(
            f"| {row['circuit_name']} | {row['stage_name']} | {row['decision']} | "
            f"{row['hellinger'] if row['hellinger'] is not None else 'missing'} | "
            f"{row['tvd'] if row['tvd'] is not None else 'missing'} | {row['executed_shots']} |"
        )
    summary_lines += ["", "## Repeated Stage 2", "", "| Circuit | n | Mean Hellinger | Sample SD | Passes | Pass rate |", "|---|---:|---:|---:|---:|---:|"]
    for row in repeated_summary:
        summary_lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]} |")
    summary_lines += ["", "## Threshold Pass Rates", "", "| Circuit | Threshold | n | Passes | Pass rate |", "|---|---:|---:|---:|---:|"]
    for row in threshold_summary:
        summary_lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} |")
    summary_lines += ["", "## Provenance", "", "```json", json.dumps(manifest, indent=2, default=list), "```", "",
                      "These experiments use AerSimulator and IBM fake backends. No real QPU execution is represented."]
    (output_dir / "summary.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    html_rows = "".join(
        f"<tr><td>{html.escape(row['circuit_name'])}</td><td>{html.escape(row['stage_name'])}</td>"
        f"<td>{html.escape(row['decision'])}</td><td>{row['hellinger'] if row['hellinger'] is not None else '-'}</td>"
        f"<td>{row['tvd'] if row['tvd'] is not None else '-'}</td></tr>" for row in pipeline_rows
    )
    (output_dir / "report.html").write_text(
        f"<html><body><h1>QPromote selected-run report</h1><p>{html.escape(json.dumps(manifest, default=list))}</p>"
        f"<h2>Pipeline {html.escape(pipeline_run)}</h2><table><tr><th>Circuit</th><th>Stage</th><th>Decision</th><th>Hellinger</th><th>TVD</th></tr>{html_rows}</table>"
        f"<p><img src='repeated_mean_hellinger.svg'><img src='repeated_pass_rate.svg'><img src='threshold_pass_rate.svg'></p></body></html>",
        encoding="utf-8",
    )
    return manifest


def noisy_decision(hellinger: float, tvd: float, min_hellinger: float, max_tvd: float) -> str:
    return "PASS" if hellinger >= min_hellinger and tvd <= max_tvd else "BLOCK"


# ── Pipeline Execution ─────────────────────────────────────────────────────────

def run_stage(
    run_id: str,
    circuit_name: str,
    stage_name: str,
    stage_type: str,
    backend_name: str,
    circuit: QuantumCircuit,
    thresholds: Dict[str, float],
    ideal_dist: Dict[str, float] | None,
    requested_shots: int = 4096,
    seed_simulator: int | None = None,
    seed_transpiler: int | None = None,
    config_id: str = "unknown",
) -> Dict[str, Any]:
    """Execute one pipeline stage and evaluate quality gate."""
    backend_label, backend = resolve_backend(backend_name)
    provenance = source_provenance()

    # Transpile and execute
    t_qc = transpile(
        circuit, backend, optimization_level=1,
        seed_transpiler=seed_transpiler,
    )
    result = backend.run(
        t_qc, shots=requested_shots, seed_simulator=seed_simulator,
    ).result()
    counts = result.get_counts()

    # Use num_clbits for normalization — critical fix for BV (3q/2c mismatch)
    n_bits = circuit.num_clbits
    measured = normalize_counts(counts, n_bits)

    # Metrics
    ref = ideal_dist or {}
    h_score  = hellinger_fidelity(measured, ref)
    tvd_val  = total_variation_distance(measured, ref)
    similarity = max(0.0, 1.0 - tvd_val)
    gc       = count_quantum_operations(circuit)
    cfp_val  = compute_cfp(circuit)

    # Gate decision
    if stage_type == "proxy":
        decision = "DEMONSTRATION"
        notes = "Hardware proxy stage. FakeSherbrooke used as QPU demonstration."
    elif stage_type == "noisy":
        min_h   = float(thresholds.get("hellinger_fidelity", 0.90))
        max_tvd = float(thresholds.get("tvd", 0.10))
        decision = noisy_decision(h_score, tvd_val, min_h, max_tvd)
        notes = (f"Noisy gate: H>={min_h:.2f}, TVD<={max_tvd:.2f}. "
                 f"Got H={h_score:.4f}, TVD={tvd_val:.4f}.")
    else:  # ideal
        min_fid = float(thresholds.get("fidelity", 0.90))
        decision = "PASS" if similarity >= min_fid else "BLOCK"
        notes = f"Ideal distribution-similarity score>={min_fid:.2f}. Got {similarity:.4f}."

    return {
        "run_id":          run_id,
        "timestamp":      utc_timestamp(),
        "circuit_name":   circuit_name,
        "stage_name":     stage_name,
        "stage_type":     stage_type,
        "backend_name":   backend_label,
        "shots":          requested_shots,
        "requested_shots": requested_shots,
        "executed_shots": requested_shots,
        "hellinger":      round(h_score, 6),
        "tvd":            round(tvd_val, 6),
        "distribution_similarity": round(similarity, 6),
        "gate_count":     gc,
        "qubit_count":    circuit.num_qubits,
        "classical_bits": circuit.num_clbits,
        "cfp":            cfp_val,
        "aer_version":    dependency_versions()[0],
        "runtime_version": dependency_versions()[1],
        "seed_simulator": seed_simulator,
        "seed_transpiler": seed_transpiler,
        "raw_counts":     counts,
        "reference_distribution": ref,
        "code_commit":    git_commit(),
        "working_tree_dirty": provenance["working_tree_dirty"],
        "source_sha256":  provenance["source_sha256"],
        "config_identity": config_id,
        "decision":       decision,
        "notes":          notes,
        "_measured_dist": measured,   # internal — for QFT Stage 2 baseline
    }


def run_pipeline(pipeline_path: Path) -> List[Dict[str, Any]]:
    """Load YAML config and execute the full pipeline."""
    cfg = load_config(pipeline_path)
    db_path       = Path(cfg.get("db_path", "qpromote_evidence.db"))
    conn          = init_db(db_path)
    run_id        = new_run_id()
    shots         = int(cfg.get("shots", 4096))
    base_seed     = int(cfg.get("seed", 20260920))
    config_id     = config_identity(cfg)
    halt_on_block = bool(cfg.get("halt_on_block", False))
    report_path   = Path(cfg.get("report_path", "qpromote_report.html"))
    records: List[Dict[str, Any]] = []
    decisions: Dict[str, str] = {}

    for stage_index, stage_cfg in enumerate(cfg.get("stages", [])):
        stage_name   = str(stage_cfg.get("name", "stage"))
        stage_type   = str(stage_cfg["type"])
        backend_name = str(stage_cfg.get("backend", "aer_simulator"))
        circuit_name = str(stage_cfg.get("circuit", "bell")).lower()
        thresholds   = stage_cfg.get("thresholds", {}) or {}
        circuit    = CIRCUIT_REGISTRY[circuit_name]()
        ideal_dist = IDEAL_DISTRIBUTIONS.get(circuit_name)

        prior_type = {"noisy": "ideal", "proxy": "noisy"}.get(stage_type)
        if prior_type and decisions.get(circuit_name) != "PASS":
            rec = make_skipped_record(
                run_id, circuit_name, stage_name, stage_type, backend_name,
                circuit, shots, config_id, f"Prior {prior_type} stage did not PASS; execution skipped.",
            )
            store_evidence(conn, rec)
            records.append(rec)
            decisions[circuit_name] = "SKIPPED"
            print(f"  - {stage_name} | {backend_name:<18} | SKIPPED (prior stage did not PASS)")
            continue

        rec = run_stage(
            run_id=run_id,
            circuit_name=circuit_name,
            stage_name=stage_name,
            stage_type=stage_type,
            backend_name=backend_name,
            circuit=circuit,
            thresholds=thresholds,
            ideal_dist=ideal_dist,
            requested_shots=shots,
            seed_simulator=make_seed(base_seed, "sim", stage_index),
            seed_transpiler=make_seed(base_seed, "transpile", stage_index),
            config_id=config_id,
        )

        # Persist (drop internal key before storage)
        storage_rec = {k: v for k, v in rec.items() if not k.startswith("_")}
        store_evidence(conn, storage_rec)
        records.append(rec)
        decisions[circuit_name] = rec["decision"]

        flag = "✓" if rec["decision"] in ("PASS", "DEMONSTRATION") else "✗"
        print(f"  {flag} {stage_name} | {rec['backend_name']:<18} | "
              f"H={rec['hellinger']:.4f}  TVD={rec['tvd']:.4f}  "
              f"CFP={rec['cfp']:<4} → {rec['decision']}")

        # Optionally stop the entire run, while default behavior continues
        # with independent circuits whose own promotion state is tracked.
        if rec["decision"] == "BLOCK" and halt_on_block:
            print(f"\n  ⛔ Pipeline halted at {stage_name}: {rec['notes']}")
            break

    conn.close()
    write_html_report(records, report_path)
    print(f"Report written to: {report_path}")
    return records


def make_skipped_record(
    run_id: str,
    circuit_name: str,
    stage_name: str,
    stage_type: str,
    backend_name: str,
    circuit: QuantumCircuit,
    requested_shots: int,
    config_id: str,
    notes: str,
) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "timestamp": utc_timestamp(),
        "circuit_name": circuit_name,
        "stage_name": stage_name,
        "stage_type": stage_type,
        "backend_name": backend_name,
        "shots": 0,
        "requested_shots": requested_shots,
        "executed_shots": 0,
        "hellinger": None,
        "tvd": None,
        "distribution_similarity": None,
        "gate_count": count_quantum_operations(circuit),
        "qubit_count": circuit.num_qubits,
        "classical_bits": circuit.num_clbits,
        "cfp": compute_cfp(circuit),
        "aer_version": dependency_versions()[0],
        "runtime_version": dependency_versions()[1],
        "seed_simulator": None,
        "seed_transpiler": None,
        "raw_counts": {},
        "reference_distribution": IDEAL_DISTRIBUTIONS[circuit_name],
        "code_commit": git_commit(),
        "working_tree_dirty": source_provenance()["working_tree_dirty"],
        "source_sha256": hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest(),
        "config_identity": config_id,
        "decision": "SKIPPED",
        "notes": notes,
    }


def load_config(pipeline_path: Path) -> Dict[str, Any]:
    if not pipeline_path.exists():
        raise FileNotFoundError(f"Pipeline file not found: {pipeline_path}")
    with pipeline_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    validate_config(cfg)
    return cfg


def validate_config(cfg: Dict[str, Any]) -> None:
    validate_positive_int(cfg.get("shots", 4096), "shots")
    if "seed" in cfg:
        validate_positive_int(cfg["seed"], "seed")
    if "halt_on_block" in cfg and type(cfg["halt_on_block"]) is not bool:
        raise ValueError("halt_on_block must be a boolean")
    stages = cfg.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("stages must be a non-empty list")
    grouped: Dict[str, List[str]] = {}
    for stage in stages:
        if not isinstance(stage, dict):
            raise ValueError("each stage must be a mapping")
        stage_type = stage.get("type")
        circuit_name = str(stage.get("circuit", "")).lower()
        if stage_type not in SUPPORTED_BACKENDS:
            raise ValueError(f"stage '{stage.get('name', '')}' must have type ideal, noisy, or proxy")
        if circuit_name not in CIRCUIT_REGISTRY:
            raise ValueError(f"unsupported circuit '{circuit_name}'")
        if not stage.get("backend"):
            raise ValueError(f"stage '{stage.get('name', '')}' must define backend")
        backend_key = canonical_backend(str(stage["backend"]))
        expected_backend = SUPPORTED_BACKENDS[stage_type]
        aliases = {
            "aer_simulator": {"aer", "aer_simulator", "simulator"},
            "fakemanilav2": {"fakemanilav2", "manila", "fake_manila_v2"},
            "fakesherbrooke": {"fakesherbrooke", "sherbrooke", "fake_sherbrooke"},
        }
        if backend_key not in aliases[expected_backend]:
            raise ValueError(
                f"stage '{stage.get('name', '')}' type '{stage_type}' requires "
                f"backend compatible with {expected_backend}; got '{stage['backend']}'"
            )
        thresholds = stage.get("thresholds", {}) or {}
        if not isinstance(thresholds, dict):
            raise ValueError(f"thresholds for '{stage.get('name', '')}' must be a mapping")
        allowed_keys = {
            "ideal": {"fidelity"},
            "noisy": {"hellinger_fidelity", "tvd"},
            "proxy": set(),
        }[stage_type]
        unknown = set(thresholds) - allowed_keys
        if unknown:
            raise ValueError(
                f"unknown threshold key(s) for {stage_type} stage '{stage.get('name', '')}': "
                + ", ".join(sorted(unknown))
            )
        if stage_type == "proxy" and thresholds:
            raise ValueError(f"proxy stage '{stage.get('name', '')}' must not define thresholds")
        for key, value in thresholds.items():
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"threshold '{key}' must be numeric") from exc
            if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
                raise ValueError(f"threshold '{key}' must be between 0 and 1")
        grouped.setdefault(circuit_name, []).append(stage_type)
    for circuit_name, types in grouped.items():
        if types != ["ideal", "noisy", "proxy"]:
            raise ValueError(
                f"stages for '{circuit_name}' must appear once in order ideal, noisy, proxy"
            )


def stage_configurations(cfg: Dict[str, Any], stage_number: str) -> Dict[str, Dict[str, Any]]:
    result = {}
    for stage_cfg in cfg.get("stages", []):
        circuit_name = str(stage_cfg.get("circuit", "")).lower()
        if stage_cfg.get("type") == stage_number and circuit_name in CIRCUIT_REGISTRY:
            result[circuit_name] = stage_cfg
    return result


def stage2_baseline(
    circuit_name: str,
    cfg: Dict[str, Any],
    run_id: str,
    shots: int,
) -> Dict[str, float] | None:
    """Return the independent expected distribution for a circuit."""
    return IDEAL_DISTRIBUTIONS[circuit_name]


def run_repeated(pipeline_path: Path, repetitions: int) -> List[Dict[str, Any]]:
    """Run Stage 2 repeatedly for every configured circuit."""
    validate_positive_int(repetitions, "--runs")
    cfg = load_config(pipeline_path)
    db_path = Path(cfg.get("db_path", "qpromote_evidence.db"))
    conn = init_db(db_path)
    run_id = new_run_id()
    shots = int(cfg.get("shots", 4096))
    base_seed = int(cfg.get("seed", 20260920))
    config_id = config_identity(cfg)
    stage2_configs = stage_configurations(cfg, "noisy")
    records: List[Dict[str, Any]] = []

    for circuit_name, stage_cfg in stage2_configs.items():
        baseline = stage2_baseline(circuit_name, cfg, run_id, shots)
        for iteration in range(1, repetitions + 1):
            rec = run_stage(
                run_id=run_id,
                circuit_name=circuit_name,
                stage_name=f"{stage_cfg.get('name', 'stage2')}_repeat_{iteration:02d}",
                stage_type="noisy",
                backend_name=str(stage_cfg.get("backend", "FakeManilaV2")),
                circuit=CIRCUIT_REGISTRY[circuit_name](),
                thresholds=stage_cfg.get("thresholds", {}) or {},
                ideal_dist=baseline,
                requested_shots=shots,
                seed_simulator=make_seed(base_seed, "repeated", circuit_name, iteration, "sim"),
                seed_transpiler=make_seed(base_seed, "repeated", circuit_name, iteration, "transpile"),
                config_id=config_id,
            )
            store_evidence(conn, {k: v for k, v in rec.items() if not k.startswith("_")})
            records.append(rec)
    conn.close()
    print(f"Repeated Stage 2 run {run_id}: {len(records)} records written")
    return records


def run_threshold_scan(pipeline_path: Path, thresholds: List[float], repetitions: int = 20) -> None:
    """Evaluate Stage 2 at each Hellinger threshold and store scan results."""
    if not thresholds:
        raise ValueError("At least one threshold is required")
    validate_positive_int(repetitions, "--runs")
    cfg = load_config(pipeline_path)
    db_path = Path(cfg.get("db_path", "qpromote_evidence.db"))
    conn = init_db(db_path)
    run_id = new_run_id()
    shots = int(cfg.get("shots", 4096))
    base_seed = int(cfg.get("seed", 20260920))
    config_id = config_identity(cfg)
    stage2_configs = stage_configurations(cfg, "noisy")
    count = 0

    for circuit_name, stage_cfg in stage2_configs.items():
        baseline = stage2_baseline(circuit_name, cfg, run_id, shots)
        configured = stage_cfg.get("thresholds", {}) or {}
        max_tvd = float(configured.get("tvd", 0.10))
        for iteration in range(1, repetitions + 1):
            rec = run_stage(
                run_id=run_id,
                circuit_name=circuit_name,
                stage_name=f"{stage_cfg.get('name', 'stage2')}_scan_{iteration:02d}",
                stage_type="noisy",
                backend_name=str(stage_cfg.get("backend", "FakeManilaV2")),
                circuit=CIRCUIT_REGISTRY[circuit_name](),
                thresholds={"hellinger_fidelity": 0.0, "tvd": max_tvd},
                ideal_dist=baseline,
                requested_shots=shots,
                seed_simulator=make_seed(base_seed, "threshold", circuit_name, iteration, "sim"),
                seed_transpiler=make_seed(base_seed, "threshold", circuit_name, iteration, "transpile"),
                config_id=config_id,
            )
            execution_id = store_scan_execution(conn, rec, iteration)
            for threshold in thresholds:
                threshold_rec = dict(rec)
                threshold_rec["decision"] = noisy_decision(
                    rec["hellinger"], rec["tvd"], threshold, max_tvd,
                )
                store_threshold_result(conn, threshold_rec, threshold, iteration, execution_id)
                count += 1
    conn.close()
    print(f"Threshold scan {run_id}: {count} records written to threshold_scan")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"QPromote v{VERSION} — Quantum Progressive Delivery Pipeline"
    )
    sub = parser.add_subparsers(dest="command")
    run_p = sub.add_parser("run", help="Execute a pipeline")
    run_p.add_argument("pipeline", nargs="?", default="pipeline.yaml",
                       help="Path to YAML pipeline file")
    repeated_p = sub.add_parser(
        "repeated-run",
        help="Repeat Stage 2 for every configured circuit",
    )
    repeated_p.add_argument("pipeline", nargs="?", default="pipeline.yaml",
                            help="Path to YAML pipeline file")
    repeated_p.add_argument("--runs", type=int, default=20,
                            help="Number of Stage 2 repetitions per circuit")
    scan_p = sub.add_parser(
        "threshold-scan",
        help="Evaluate Stage 2 across Hellinger thresholds",
    )
    scan_p.add_argument("pipeline", nargs="?", default="pipeline.yaml",
                        help="Path to YAML pipeline file")
    scan_p.add_argument(
        "--thresholds",
        required=True,
        help="Comma-separated thresholds, for example 0.80,0.85,0.90,0.925,0.95",
    )
    scan_p.add_argument("--runs", type=int, default=20,
                        help="Number of sampled executions per circuit")
    report_p = sub.add_parser("report", help="Generate reports from selected runs")
    report_p.add_argument("--db", required=True, help="SQLite evidence database")
    report_p.add_argument("--output", required=True, help="Output directory")
    report_p.add_argument("--pipeline-run", required=True)
    report_p.add_argument("--repeated-run", required=True)
    report_p.add_argument("--threshold-run", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "run":
            print(f"\nQPromote v{VERSION} — starting pipeline: {args.pipeline}\n")
            records = run_pipeline(Path(args.pipeline))
            config = load_config(Path(args.pipeline))
            print(f"\nDone. Evidence stored in: {config.get('db_path', 'qpromote_evidence.db')}")
            return 0
        elif args.command == "repeated-run":
            run_repeated(Path(args.pipeline), args.runs)
            return 0
        elif args.command == "threshold-scan":
            validate_positive_int(args.runs, "--runs")
            thresholds = validate_scan_thresholds(args.thresholds.split(","))
            run_threshold_scan(Path(args.pipeline), thresholds, args.runs)
            return 0
        elif args.command == "report":
            manifest = generate_reports(
                Path(args.db), Path(args.output), args.pipeline_run,
                args.repeated_run, args.threshold_run,
            )
            print(json.dumps(manifest, indent=2, default=list))
            return 0
        parser.print_help()
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
