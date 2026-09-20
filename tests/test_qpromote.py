import tempfile
import unittest
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

    def test_invalid_stage_type_and_order_are_rejected(self):
        with self.assertRaises(ValueError):
            qpromote.validate_config({
                "shots": 4096,
                "stages": [{"name": "stage1", "type": "stage1", "backend": "aer_simulator", "circuit": "bell"}],
            })
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


if __name__ == "__main__":
    unittest.main()
