# QPromote Selected-Run Summary

- Database: `evidence_20260920_current2.db`
- Pipeline run: `7b176f65`
- Repeated run: `2199b078`
- Threshold run: `7202f6c9`
- Pipeline records: 15
- Repeated records: 100
- Actual scan executions: 100
- Threshold decisions: 500
- Actual scan shots: 409600

## Pipeline

| Circuit | Stage | Decision | Hellinger | TVD | Executed shots |
|---|---|---|---:|---:|---:|
| bell | stage1_bell | PASS | 0.999879 | 0.010986 | 4096 |
| bell | stage2_bell | PASS | 0.938999 | 0.060791 | 4096 |
| bell | stage3_bell | DEMONSTRATION | 0.957495 | 0.04248 | 4096 |
| ghz | stage1_ghz | PASS | 0.999863 | 0.011719 | 4096 |
| ghz | stage2_ghz | BLOCK | 0.872012 | 0.127686 | 4096 |
| ghz | stage3_ghz | SKIPPED | missing | missing | 0 |
| grover | stage1_grover | PASS | 1.0 | 0.0 | 4096 |
| grover | stage2_grover | BLOCK | 0.897217 | 0.102783 | 4096 |
| grover | stage3_grover | SKIPPED | missing | missing | 0 |
| bv | stage1_bv | PASS | 1.0 | 0.0 | 4096 |
| bv | stage2_bv | PASS | 0.931152 | 0.068848 | 4096 |
| bv | stage3_bv | DEMONSTRATION | 0.9729 | 0.0271 | 4096 |
| qft | stage1_qft | PASS | 1.0 | 0.0 | 4096 |
| qft | stage2_qft | BLOCK | 0.850586 | 0.149414 | 4096 |
| qft | stage3_qft | SKIPPED | missing | missing | 0 |

## Repeated Stage 2

| Circuit | n | Mean Hellinger | Sample SD | Passes | Pass rate |
|---|---:|---:|---:|---:|---:|
| bell | 20 | 0.938719 | 0.003554 | 20 | 1.0000 |
| bv | 20 | 0.927820 | 0.003367 | 20 | 1.0000 |
| ghz | 20 | 0.868675 | 0.005253 | 0 | 0.0000 |
| grover | 20 | 0.897424 | 0.004354 | 4 | 0.2000 |
| qft | 20 | 0.852576 | 0.005074 | 0 | 0.0000 |

## Threshold Pass Rates

| Circuit | Threshold | n | Passes | Pass rate |
|---|---:|---:|---:|---:|
| bell | 0.8 | 20 | 20 | 1.0000 |
| bell | 0.85 | 20 | 20 | 1.0000 |
| bell | 0.9 | 20 | 20 | 1.0000 |
| bell | 0.925 | 20 | 20 | 1.0000 |
| bell | 0.95 | 20 | 0 | 0.0000 |
| bv | 0.8 | 20 | 20 | 1.0000 |
| bv | 0.85 | 20 | 20 | 1.0000 |
| bv | 0.9 | 20 | 20 | 1.0000 |
| bv | 0.925 | 20 | 17 | 0.8500 |
| bv | 0.95 | 20 | 0 | 0.0000 |
| ghz | 0.8 | 20 | 0 | 0.0000 |
| ghz | 0.85 | 20 | 0 | 0.0000 |
| ghz | 0.9 | 20 | 0 | 0.0000 |
| ghz | 0.925 | 20 | 0 | 0.0000 |
| ghz | 0.95 | 20 | 0 | 0.0000 |
| grover | 0.8 | 20 | 6 | 0.3000 |
| grover | 0.85 | 20 | 6 | 0.3000 |
| grover | 0.9 | 20 | 6 | 0.3000 |
| grover | 0.925 | 20 | 0 | 0.0000 |
| grover | 0.95 | 20 | 0 | 0.0000 |
| qft | 0.8 | 20 | 0 | 0.0000 |
| qft | 0.85 | 20 | 0 | 0.0000 |
| qft | 0.9 | 20 | 0 | 0.0000 |
| qft | 0.925 | 20 | 0 | 0.0000 |
| qft | 0.95 | 20 | 0 | 0.0000 |

## Provenance

```json
{
  "database": "evidence_20260920_current2.db",
  "pipeline_run_id": "7b176f65",
  "repeated_run_id": "2199b078",
  "threshold_run_id": "7202f6c9",
  "pipeline_records": 15,
  "repeated_records": 100,
  "scan_executions": 100,
  "threshold_decisions": 500,
  "scan_executed_shots": 409600,
  "provenance": [
    [
      "97322334c257700be5ee7a99d5ec23f83f2f9085",
      "6c5fa8595869f958",
      1,
      "cc6a80c6b9c6013c3c47d33aff2fdf4290a7e75a5bc2d340493a6d6fd2a679ef"
    ]
  ],
  "aer_versions": [
    "0.17.2"
  ],
  "runtime_versions": [
    "0.49.0"
  ]
}
```

These experiments use AerSimulator and IBM fake backends. No real QPU execution is represented.
