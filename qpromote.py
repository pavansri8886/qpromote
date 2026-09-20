#!/usr/bin/env python3
"""
QPromote — Declarative Progressive Delivery Pipeline for Quantum Circuits
Version: 1.3.0
Authors: Hassan Soubra, Pavan Kumar Naganaboina, Samuel Richard,
         Tuna Hacaloglu, Donatien Koulla Moulla, Pierre Bourque, Alain Abran

Usage:
    python qpromote.py run pipeline.yaml
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
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

VERSION = "1.3.0"

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
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def config_identity(cfg: Dict[str, Any]) -> str:
    payload = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


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
    runtime_version  TEXT    NOT NULL
    ,seed_simulator  INTEGER
    ,seed_transpiler INTEGER
    ,raw_counts      TEXT
    ,reference_distribution TEXT
    ,code_commit     TEXT
    ,config_identity TEXT
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
        "config_identity": "TEXT",
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
    }
    for column, definition in threshold_migrations.items():
        if column not in threshold_columns:
            conn.execute(f"ALTER TABLE threshold_scan ADD COLUMN {column} {definition}")
    conn.commit()
    return conn


def store_evidence(conn: sqlite3.Connection, rec: Dict[str, Any]) -> None:
    conn.execute("""
        INSERT INTO evidence
            (run_id, timestamp, circuit_name, stage_name, stage_type, backend_name, shots,
             requested_shots, executed_shots, hellinger, tvd,
             distribution_similarity, gate_count, qubit_count, classical_bits,
             cfp, aer_version, runtime_version, seed_simulator, seed_transpiler,
             raw_counts, reference_distribution, code_commit, config_identity,
             decision, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        rec.get("code_commit"), rec.get("config_identity"),
        rec["decision"], rec.get("notes"),
    ))
    conn.commit()


def store_threshold_result(
    conn: sqlite3.Connection,
    rec: Dict[str, Any],
    threshold: float,
    repetition: int,
) -> None:
    conn.execute("""
        INSERT INTO threshold_scan
            (run_id, timestamp, circuit_name, repetition, threshold, shots,
               requested_shots, executed_shots, hellinger, tvd, decision,
               aer_version, runtime_version, seed_simulator, seed_transpiler,
               raw_counts, reference_distribution, code_commit, config_identity)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        rec["run_id"], rec["timestamp"], rec["circuit_name"], repetition, threshold,
        rec["shots"], rec["requested_shots"], rec["executed_shots"],
        rec.get("hellinger"), rec.get("tvd"), rec["decision"],
        rec["aer_version"], rec["runtime_version"], rec.get("seed_simulator"),
        rec.get("seed_transpiler"), json.dumps(rec.get("raw_counts", {}), sort_keys=True),
        json.dumps(rec.get("reference_distribution", {}), sort_keys=True),
        rec.get("code_commit"), rec.get("config_identity"),
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
    if not isinstance(cfg.get("shots", 4096), int) or int(cfg.get("shots", 4096)) <= 0:
        raise ValueError("shots must be a positive integer")
    stages = cfg.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ValueError("stages must be a non-empty list")
    expected_order = {"ideal": 0, "noisy": 1, "proxy": 2}
    grouped: Dict[str, List[str]] = {}
    for stage in stages:
        if not isinstance(stage, dict):
            raise ValueError("each stage must be a mapping")
        stage_type = stage.get("type")
        circuit_name = str(stage.get("circuit", "")).lower()
        if stage_type not in expected_order:
            raise ValueError(f"stage '{stage.get('name', '')}' must have type ideal, noisy, or proxy")
        if circuit_name not in CIRCUIT_REGISTRY:
            raise ValueError(f"unsupported circuit '{circuit_name}'")
        if not stage.get("backend"):
            raise ValueError(f"stage '{stage.get('name', '')}' must define backend")
        thresholds = stage.get("thresholds", {}) or {}
        for key, value in thresholds.items():
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"threshold '{key}' must be numeric") from exc
            if not 0.0 <= numeric <= 1.0:
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
    if repetitions < 1:
        raise ValueError("--runs must be at least 1")
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
    if repetitions < 1:
        raise ValueError("--runs must be at least 1")
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
            for threshold in thresholds:
                threshold_rec = dict(rec)
                threshold_rec["decision"] = noisy_decision(
                    rec["hellinger"], rec["tvd"], threshold, max_tvd,
                )
                store_threshold_result(conn, threshold_rec, threshold, iteration)
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
            thresholds = [float(value.strip()) for value in args.thresholds.split(",") if value.strip()]
            run_threshold_scan(Path(args.pipeline), thresholds, args.runs)
            return 0
        parser.print_help()
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
