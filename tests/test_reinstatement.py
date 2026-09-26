import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, ValidationError


CREATE_DATA = {'event_id': 'CAT-2026-01', 'attachment': 1000000.0, 'limit': 5000000.0, 'cession_pct': 0.4, 'loss_amount': 3000000.0, 'reinstatement_pct': 0.15, 'aggregate_prior': 0.0, 'reinstatement_count': 1}
UW = Actor("uw", "underwriter")
CO = Actor("co", "claims_officer")
FIN = Actor("fin", "finance")


class ReinstatementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference="RI-1", **overrides):
        data = dict(CREATE_DATA)
        data.update(overrides)
        return self.service.create(UW, reference, data)

    def _advance_to_calculated(self, record, approved_loss=2800000.0):
        record = self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-8"})
        record = self.service.act(CO, record["id"], record["version"], "submit_claim", {"claim_number": "CLM-1", "event_id": "CAT-2026-01"})
        record = self.service.act(CO, record["id"], record["version"], "calculate", {"approved_loss": approved_loss})
        return record

    def test_settle_consumes_count_and_accumulates_premium(self):
        record = self._create()
        detail = self.service.reinstatement_detail(UW, "CAT-2026-01")
        self.assertEqual(detail["ledger"]["total_count"], 1)
        self.assertEqual(detail["ledger"]["used_count"], 0)
        self.assertEqual(detail["ledger"]["remaining_count"], 1)
        self.assertEqual(detail["ledger"]["accumulated_premium"], 0)
        self.assertEqual(detail["entries"], [])
        record = self._advance_to_calculated(record)
        record = self.service.act(FIN, record["id"], record["version"], "settle", {"payment_reference": "PAY-1"})
        self.assertEqual(record["reinstatement"]["used_count"], 1)
        self.assertEqual(record["reinstatement"]["remaining_count"], 0)
        self.assertEqual(record["reinstatement"]["accumulated_premium"], 108000.0)
        detail = self.service.reinstatement_detail(UW, "CAT-2026-01")
        self.assertEqual(len(detail["entries"]), 1)
        entry = detail["entries"][0]
        self.assertEqual(entry["record_id"], record["id"])
        self.assertEqual(entry["recovery_amount"], 720000.0)
        self.assertEqual(entry["premium"], 108000.0)

    def test_intake_snapshot_and_exhaustion_blocks_calculate(self):
        first = self._advance_to_calculated(self._create("RI-1"))
        self.service.act(FIN, first["id"], first["version"], "settle", {"payment_reference": "PAY-1"})
        second = self._create("RI-2")
        second = self.service.act(UW, second["id"], second["version"], "bind", {"underwriter_id": "UW-8"})
        second = self.service.act(CO, second["id"], second["version"], "submit_claim", {"claim_number": "CLM-2", "event_id": "CAT-2026-01"})
        snapshot = second["payload"]["reinstatement_snapshot"]
        self.assertEqual(snapshot["used_count"], 1)
        self.assertEqual(snapshot["remaining_count"], 0)
        self.assertEqual(snapshot["accumulated_premium"], 108000.0)
        with self.assertRaises(Conflict) as ctx:
            self.service.act(CO, second["id"], second["version"], "calculate", {"approved_loss": 1500000.0})
        message = str(ctx.exception)
        self.assertIn("已用1/1次", message)
        self.assertIn("缺少1次", message)
        self.assertIn("108000", message)

    def test_rejected_and_unsettled_do_not_consume(self):
        first = self._advance_to_calculated(self._create("RI-1"))
        self.service.act(FIN, first["id"], first["version"], "reject", {"reject_reason": "材料不全"})
        ledger = self.service.reinstatement_detail(UW, "CAT-2026-01")["ledger"]
        self.assertEqual(ledger["used_count"], 0)
        second = self._advance_to_calculated(self._create("RI-2"))
        ledger = self.service.reinstatement_detail(UW, "CAT-2026-01")["ledger"]
        self.assertEqual(ledger["used_count"], 0)
        second = self.service.act(FIN, second["id"], second["version"], "settle", {"payment_reference": "PAY-2"})
        self.assertEqual(second["reinstatement"]["used_count"], 1)

    def test_settle_fails_when_count_exhausted_by_other_case(self):
        first = self._advance_to_calculated(self._create("RI-1"))
        second = self._advance_to_calculated(self._create("RI-2"))
        self.service.act(FIN, first["id"], first["version"], "settle", {"payment_reference": "PAY-1"})
        with self.assertRaises(Conflict):
            self.service.act(FIN, second["id"], second["version"], "settle", {"payment_reference": "PAY-2"})

    def test_ledger_survives_service_restart(self):
        record = self._advance_to_calculated(self._create("RI-1"))
        self.service.act(FIN, record["id"], record["version"], "settle", {"payment_reference": "PAY-1"})
        restarted = build_service(self.db)
        detail = restarted.reinstatement_detail(UW, "CAT-2026-01")
        self.assertEqual(detail["ledger"]["used_count"], 1)
        self.assertEqual(detail["ledger"]["accumulated_premium"], 108000.0)
        self.assertEqual(len(detail["entries"]), 1)
        fetched = restarted.get_record(UW, record["id"])
        self.assertEqual(fetched["reinstatement"]["used_count"], 1)
        self.assertEqual(fetched["reinstatement"]["accumulated_premium"], 108000.0)

    def test_conflicting_registration_rejected(self):
        self._create("RI-1")
        with self.assertRaises(Conflict):
            self._create("RI-2", reinstatement_count=3)

    def test_reinstatement_count_must_be_positive_integer(self):
        with self.assertRaises(ValidationError):
            self._create("RI-1", reinstatement_count=0)

    def test_unknown_event_detail_is_not_found(self):
        with self.assertRaises(NotFound):
            self.service.reinstatement_detail(UW, "CAT-UNKNOWN")


if __name__ == "__main__":
    unittest.main()
