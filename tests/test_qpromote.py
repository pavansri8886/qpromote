import tempfile
import unittest
import json
import sqlite3
from pathlib import Path

import qpromote
from qiskit_aer import AerSimulator
from qiskit import transpile


class QPromoteCorrectnessTests(unittest.TestCase):
    def test_cfp_counts_quantum_gates_and_measurements_once(self):
        self.assertEqual(qpromote.count_quantum_operations(qpromote.bell_circuit()), 2)
        self.assertEqual(qpromote.count_measurements(qpromote.bell_circuit()), 2)
        self.assertEqual(qpromote.compute_cfp(qpromote.bell_circuit()), 8)

    def test_qft_uniform_input_has_zero_output(self):
        circuit = qpromote.qft_circuit()
        backend = AerSimulator(method="statevector")
        result = backend.run(
            transpile(circuit, backend, seed_transpiler=7),
            shots=128,
            seed_simulator=11,
        ).result()
        counts = result.get_counts()
        self.assertEqual(set(counts), {"000"})
        self.assertEqual(qpromote.IDEAL_DISTRIBUTIONS["qft"], {"000": 1.0})

    def test_metric_known_values(self):
        self.assertAlmostEqual(
            qpromote.hellinger_fidelity({"00": 0.5, "11": 0.5}, {"00": 0.5, "11": 0.5}),
            1.0,
        )
        self.assertAlmostEqual(
            qpromote.total_variation_distance({"00": 1.0}, {"11": 1.0}),
            1.0,
        )
        self.assertEqual(
            qpromote.recalculate_metrics({"11": 4096}, {"11": 1.0}, 2),
            (1.0, 0.0, 1.0),
        )

    def test_invalid_stage_type_and_order_are_rejected(self):
        with self.assertRaises(ValueError):
            qpromote.validate_config({
                "shots": 4096,
                "stages": [{"name": "stage1", "type": "stage1", "backend": "aer_simulator", "circuit": "bell"}],
            })

    def test_invalid_keys_backends_booleans_and_scan_thresholds_are_rejected(self):
        base = {
            "shots": 4096,
            "seed": 7,
            "halt_on_block": False,
            "stages": [
                {"name": "ideal", "type": "ideal", "backend": "aer_simulator", "circuit": "bell", "thresholds": {"fidelity": 0.9}},
                {"name": "noisy", "type": "noisy", "backend": "FakeManilaV2", "circuit": "bell", "thresholds": {"hellinger_fidelty": 0.9}},
                {"name": "proxy", "type": "proxy", "backend": "FakeSherbrooke", "circuit": "bell"},
            ],
        }
        with self.assertRaisesRegex(ValueError, "unknown threshold"):
            qpromote.validate_config(base)
        base["stages"][1]["thresholds"] = {"hellinger_fidelity": 0.9, "tvd": 0.1}
        base["stages"][1]["backend"] = "aer_simulator"
        with self.assertRaisesRegex(ValueError, "requires backend"):
            qpromote.validate_config(base)
        base["stages"][1]["backend"] = "FakeManilaV2"
        base["halt_on_block"] = 1
        with self.assertRaisesRegex(ValueError, "halt_on_block"):
            qpromote.validate_config(base)
        for values in (["nan"], ["inf"], ["-0.1"], ["1.1"], ["0.9", "0.90"]):
            with self.assertRaises(ValueError):
                qpromote.validate_scan_thresholds(values)

    def test_proxy_thresholds_and_seed_types_are_rejected(self):
        config = {
            "shots": True,
            "seed": 7,
            "stages": [{"type": "proxy", "backend": "FakeSherbrooke", "circuit": "bell", "thresholds": {"tvd": 0.1}}],
        }
        with self.assertRaises(ValueError):
            qpromote.validate_config(config)
        config["shots"] = 4096
        config["stages"][0]["thresholds"] = {}
        config["seed"] = True
        with self.assertRaises(ValueError):
            qpromote.validate_config(config)
        with self.assertRaises(ValueError):
            qpromote.validate_config({
                "shots": 4096,
                "stages": [
                    {"name": "stage2", "type": "noisy", "backend": "FakeManilaV2", "circuit": "bell"},
                    {"name": "stage1", "type": "ideal", "backend": "aer_simulator", "circuit": "bell"},
                    {"name": "stage3", "type": "proxy", "backend": "FakeSherbrooke", "circuit": "bell"},
                ],
            })

    def test_skipped_record_has_zero_executed_shots(self):
        record = qpromote.make_skipped_record(
            "run1234", "bell", "stage3_bell", "proxy", "FakeSherbrooke",
            qpromote.bell_circuit(), 4096, "config123", "blocked",
        )
        self.assertEqual(record["shots"], 0)
        self.assertEqual(record["requested_shots"], 4096)
        self.assertEqual(record["executed_shots"], 0)
        self.assertEqual(record["decision"], "SKIPPED")

    def test_threshold_decision_uses_same_metric_values(self):
        self.assertEqual(qpromote.noisy_decision(0.91, 0.09, 0.90, 0.10), "PASS")
        self.assertEqual(qpromote.noisy_decision(0.91, 0.09, 0.925, 0.10), "BLOCK")
        self.assertEqual(qpromote.noisy_decision(0.95, 0.11, 0.90, 0.10), "BLOCK")

    def test_stage1_block_skips_noisy_and_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = Path(directory) / "pipeline.yaml"
            database = (Path(directory) / "evidence.db").as_posix()
            config = {
                "db_path": database,
                "shots": 128,
                "seed": 7,
                "halt_on_block": False,
                "stages": [
                    {"name": "ideal_bell", "type": "ideal", "backend": "aer_simulator", "circuit": "bell", "thresholds": {"fidelity": 1.0}},
                    {"name": "noisy_bell", "type": "noisy", "backend": "FakeManilaV2", "circuit": "bell", "thresholds": {"hellinger_fidelity": 0.9, "tvd": 0.1}},
                    {"name": "proxy_bell", "type": "proxy", "backend": "FakeSherbrooke", "circuit": "bell"},
                ],
            }
            import yaml
            pipeline.write_text(yaml.safe_dump(config), encoding="utf-8")
            records = qpromote.run_pipeline(pipeline)
            self.assertEqual([record["decision"] for record in records], ["BLOCK", "SKIPPED", "SKIPPED"])
            self.assertEqual([record["executed_shots"] for record in records], [128, 0, 0])

    def test_stage2_block_skips_proxy_and_other_circuit_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "db_path": str(Path(directory) / "evidence.db"), "shots": 64, "seed": 7,
                "halt_on_block": False, "stages": [
                    {"name": "ideal_bell", "type": "ideal", "backend": "aer_simulator", "circuit": "bell", "thresholds": {"fidelity": 0.9}},
                    {"name": "noisy_bell", "type": "noisy", "backend": "FakeManilaV2", "circuit": "bell", "thresholds": {"hellinger_fidelity": 1.0, "tvd": 0.0}},
                    {"name": "proxy_bell", "type": "proxy", "backend": "FakeSherbrooke", "circuit": "bell"},
                    {"name": "ideal_bv", "type": "ideal", "backend": "aer_simulator", "circuit": "bv", "thresholds": {"fidelity": 0.9}},
                    {"name": "noisy_bv", "type": "noisy", "backend": "FakeManilaV2", "circuit": "bv", "thresholds": {"hellinger_fidelity": 0.0, "tvd": 1.0}},
                    {"name": "proxy_bv", "type": "proxy", "backend": "FakeSherbrooke", "circuit": "bv"},
                ],
            }
            import yaml
            pipeline = Path(directory) / "pipeline.yaml"
            pipeline.write_text(yaml.safe_dump(config), encoding="utf-8")
            records = qpromote.run_pipeline(pipeline)
            decisions = {(r["circuit_name"], r["stage_type"]): r for r in records}
            self.assertEqual(decisions[("bell", "noisy")]["decision"], "BLOCK")
            self.assertEqual(decisions[("bell", "proxy")]["decision"], "SKIPPED")
            self.assertEqual(decisions[("bv", "proxy")]["decision"], "DEMONSTRATION")

    def test_scan_execution_and_report_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "evidence.db"
            conn = qpromote.init_db(db)
            reference = {"11": 1.0}
            record = {
                "run_id": "scanrun", "timestamp": "now", "circuit_name": "grover",
                "stage_name": "noisy_scan", "stage_type": "noisy", "backend_name": "FakeManilaV2",
                "shots": 4096, "requested_shots": 4096, "executed_shots": 4096,
                "hellinger": 0.95, "tvd": 0.05, "distribution_similarity": 0.95,
                "gate_count": 12, "qubit_count": 2, "classical_bits": 2, "cfp": 28,
                "aer_version": "a", "runtime_version": "r", "seed_simulator": 1,
                "seed_transpiler": 2, "raw_counts": {"11": 4096},
                "reference_distribution": reference, "code_commit": "c",
                "working_tree_dirty": 0, "source_sha256": "s", "config_identity": "cfg",
                "decision": "PASS", "notes": "",
            }
            execution_id = qpromote.store_scan_execution(conn, record, 1)
            for threshold in (0.8, 0.85, 0.9, 0.925, 0.95):
                qpromote.store_threshold_result(conn, record, threshold, 1, execution_id)
            conn.close()
            check = sqlite3.connect(db)
            rows = check.execute("SELECT COUNT(*), COUNT(DISTINCT execution_id), SUM(executed_shots) FROM threshold_scan").fetchone()
            actual = check.execute("SELECT COUNT(*), SUM(executed_shots) FROM scan_executions").fetchone()
            check.close()
            self.assertEqual(rows, (5, 1, 0))
            self.assertEqual(actual, (1, 4096))
            pipeline_record = dict(record)
            pipeline_record.update({"run_id": "pipeline", "stage_name": "stage2_grover", "stage_type": "noisy"})
            repeated_record = dict(record)
            repeated_record.update({"run_id": "repeated", "stage_name": "stage2_grover_repeat_01"})
            other_record = dict(record)
            other_record.update({"run_id": "other", "stage_name": "unselected"})
            report_conn = sqlite3.connect(db)
            qpromote.store_evidence(report_conn, pipeline_record)
            qpromote.store_evidence(report_conn, repeated_record)
            qpromote.store_evidence(report_conn, other_record)
            report_conn.close()
            output = Path(directory) / "report"
            manifest = qpromote.generate_reports(db, output, "pipeline", "repeated", "scanrun")
            self.assertEqual(manifest["pipeline_records"], 1)
            self.assertTrue((output / "summary.md").exists())
            self.assertNotIn("unselected", (output / "summary.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
