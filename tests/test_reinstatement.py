import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ReinstatementExhausted, ValidationError


BASE_DATA = {
    'event_id': 'TY-2026-IN-FA',
    'attachment': 1000000.0,
    'limit': 5000000.0,
    'cession_pct': 0.4,
    'loss_amount': 2000000.0,
    'reinstatement_pct': 0.15,
    'reinstatement_count': 2,
    'aggregate_prior': 0.0,
}


class ReinstatementLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)
        self.underwriter = Actor("u1", "underwriter")
        self.claims = Actor("c1", "claims_officer")
        self.finance = Actor("f1", "finance")

    def tearDown(self):
        self.temp.cleanup()

    def _create_claim(self, reference, event_id=BASE_DATA['event_id'], loss_amount=2000000.0,
                      reinstatement_count=2, aggregate_prior=0.0):
        data = dict(BASE_DATA)
        data['event_id'] = event_id
        data['loss_amount'] = loss_amount
        data['reinstatement_count'] = reinstatement_count
        data['aggregate_prior'] = aggregate_prior
        return self.service.create(self.underwriter, reference, data)

    def _bind(self, record):
        return self.service.act(self.underwriter, record["id"], record["version"], "bind", {"underwriter_id": "UW-1"})

    def _submit(self, record, claim_number, event_id=BASE_DATA['event_id']):
        return self.service.act(self.claims, record["id"], record["version"], "submit_claim",
                                {"claim_number": claim_number, "event_id": event_id})

    def _calculate(self, record, approved_loss=2000000.0):
        return self.service.act(self.claims, record["id"], record["version"], "calculate",
                                {"approved_loss": approved_loss})

    def _settle(self, record, payment_reference):
        return self.service.act(self.finance, record["id"], record["version"], "settle",
                                {"payment_reference": payment_reference})

    def _full_flow(self, reference, claim_number, payment_reference, approved_loss=2000000.0, aggregate_prior=0.0):
        record = self._create_claim(reference, loss_amount=approved_loss, aggregate_prior=aggregate_prior)
        record = self._bind(record)
        record = self._submit(record, claim_number)
        record = self._calculate(record, approved_loss=approved_loss)
        return self._settle(record, payment_reference)

    def test_ledger_registered_on_create(self):
        record = self._create_claim("RI-30001")
        ledger = record["reinstatement"]
        self.assertEqual(ledger["event_id"], BASE_DATA['event_id'])
        self.assertEqual(ledger["total_count"], 2)
        self.assertEqual(ledger["used_count"], 0)
        self.assertEqual(ledger["available_count"], 2)
        self.assertFalse(ledger["exhausted"])
        self.assertEqual(ledger["accumulated_premium"], 0.0)

    def test_settlement_consumes_one_and_accumulates_premium(self):
        # approved_loss 2,000,000 -> recovery=(2m-1m)*0.4=400,000 -> premium=60,000
        record = self._full_flow("RI-30001", "CLM-1", "PAY-1")
        ledger = record["reinstatement"]
        self.assertEqual(record["state"], "settled")
        self.assertEqual(ledger["used_count"], 1)
        self.assertEqual(ledger["available_count"], 1)
        self.assertEqual(ledger["accumulated_recovery"], 400000.0)
        self.assertEqual(ledger["accumulated_premium"], 60000.0)

        detail = self.service.get_reinstatement(self.underwriter, BASE_DATA['event_id'])
        self.assertEqual(len(detail["entries"]), 1)
        entry = detail["entries"][0]
        self.assertEqual(entry["seq"], 1)
        self.assertEqual(entry["record_id"], record["id"])
        self.assertEqual(entry["recovery_amount"], 400000.0)
        self.assertEqual(entry["reinstatement_premium"], 60000.0)
        self.assertEqual(entry["payment_reference"], "PAY-1")

    def test_premium_accumulates_across_settlements(self):
        self._full_flow("RI-30001", "CLM-1", "PAY-1", approved_loss=2000000.0)
        record = self._full_flow("RI-30002", "CLM-2", "PAY-2", approved_loss=3000000.0,
                                 aggregate_prior=400000.0)
        # second recovery=(3m-1m)*0.4=800,000 -> premium=120,000
        ledger = record["reinstatement"]
        self.assertEqual(ledger["used_count"], 2)
        self.assertEqual(ledger["available_count"], 0)
        self.assertTrue(ledger["exhausted"])
        self.assertEqual(ledger["accumulated_recovery"], 1200000.0)
        self.assertEqual(ledger["accumulated_premium"], 180000.0)
        detail = self.service.get_reinstatement(self.underwriter, BASE_DATA['event_id'])
        self.assertEqual([entry["seq"] for entry in detail["entries"]], [1, 2])

    def test_exhausted_event_rejects_submit_with_explicit_shortage(self):
        self._full_flow("RI-30001", "CLM-1", "PAY-1")
        self._full_flow("RI-30002", "CLM-2", "PAY-2", approved_loss=3000000.0,
                        aggregate_prior=400000.0)

        # 次数耗尽，新案受理即被拒绝，错误写清已用、缺少次数与累计保费
        record = self._create_claim("RI-30003", loss_amount=1000000.0, aggregate_prior=400000.0)
        record = self._bind(record)
        with self.assertRaises(ReinstatementExhausted) as caught:
            self._submit(record, "CLM-3")
        info = caught.exception.details
        self.assertEqual(info["used_count"], 2)
        self.assertEqual(info["total_count"], 2)
        self.assertEqual(info["available_count"], 0)
        self.assertEqual(info["short_count"], 1)
        self.assertEqual(info["accumulated_premium"], 180000.0)
        self.assertIn("已用2/2次", str(caught.exception))
        self.assertIn("缺少1次", str(caught.exception))
        # 受理被拒，案件不前进，不产生消耗
        self.assertEqual(self.service.get_record(self.underwriter, record["id"])["state"], "bound")

    def test_zero_reinstatement_event_rejects_submit_and_calculate(self):
        # 合约登记零次恢复：受理环节即被拒绝
        record = self._create_claim("RI-30099", event_id="TY-NO-REINST", reinstatement_count=0)
        record = self._bind(record)
        with self.assertRaises(ReinstatementExhausted) as caught:
            self._submit(record, "CLM-99", event_id="TY-NO-REINST")
        self.assertEqual(caught.exception.details["total_count"], 0)
        self.assertEqual(caught.exception.details["short_count"], 1)

        # 核定环节同样受闸门保护：案件B在尚余1次时已受理，随后另一案结算耗尽次数，B再核定被拒
        self._full_flow("RI-40001", "CLM-A", "PAY-A")
        waiting = self._create_claim("RI-40002", loss_amount=1500000.0, aggregate_prior=400000.0)
        waiting = self._bind(waiting)
        waiting = self._submit(waiting, "CLM-B")
        self._full_flow("RI-40003", "CLM-C", "PAY-C", approved_loss=3000000.0,
                        aggregate_prior=200000.0)
        with self.assertRaises(ReinstatementExhausted) as caught:
            self._calculate(waiting)
        self.assertEqual(caught.exception.details["used_count"], 2)
        self.assertEqual(caught.exception.details["short_count"], 1)

    def test_unsettled_and_rejected_claims_do_not_consume(self):
        # 案件1走完全流程消耗1次
        self._full_flow("RI-30001", "CLM-1", "PAY-1")
        # 案件2只核定不结算，不占次数
        pending = self._create_claim("RI-30002", aggregate_prior=400000.0)
        pending = self._bind(pending)
        pending = self._submit(pending, "CLM-2")
        pending = self._calculate(pending)
        ledger = self.service.get_reinstatement(self.underwriter, BASE_DATA['event_id'])
        self.assertEqual(ledger["used_count"], 1)
        # 案件3拒赔，也不占次数
        rejected = self._create_claim("RI-30003", loss_amount=1000000.0)
        rejected = self._bind(rejected)
        rejected = self._submit(rejected, "CLM-3")
        rejected = self.service.act(self.claims, rejected["id"], rejected["version"],
                                    "reject", {"reject_reason": "非本季台风"})
        self.assertEqual(rejected["state"], "rejected")
        ledger = self.service.get_reinstatement(self.underwriter, BASE_DATA['event_id'])
        self.assertEqual(ledger["used_count"], 1)
        self.assertEqual(len(ledger["entries"]), 1)
        # 待结算案件此后仍可正常结算并消耗剩下的一次
        pending = self._settle(pending, "PAY-2")
        self.assertEqual(pending["reinstatement"]["used_count"], 2)
        self.assertEqual(pending["reinstatement"]["accumulated_premium"], 120000.0)

    def test_ledger_survives_service_restart(self):
        self._full_flow("RI-30001", "CLM-1", "PAY-1", approved_loss=2000000.0)
        restarted = build_service(self.db_path)
        ledger = restarted.get_reinstatement(self.underwriter, BASE_DATA['event_id'])
        self.assertEqual(ledger["total_count"], 2)
        self.assertEqual(ledger["used_count"], 1)
        self.assertEqual(ledger["accumulated_premium"], 60000.0)
        self.assertEqual(ledger["entries"][0]["payment_reference"], "PAY-1")
        # 重开后闸门仍然生效：剩余1次还可结算一案，再下一案不能受理
        record = restarted.create(self.underwriter, "RI-30002",
                                  {**BASE_DATA, 'loss_amount': 2000000.0, 'aggregate_prior': 400000.0})
        record = restarted.act(self.underwriter, record["id"], record["version"], "bind",
                               {"underwriter_id": "UW-1"})
        record = restarted.act(self.claims, record["id"], record["version"], "submit_claim",
                               {"claim_number": "CLM-2", "event_id": BASE_DATA['event_id']})
        record = restarted.act(self.claims, record["id"], record["version"], "calculate",
                               {"approved_loss": 2000000.0})
        record = restarted.act(self.finance, record["id"], record["version"], "settle",
                               {"payment_reference": "PAY-2"})
        self.assertEqual(record["reinstatement"]["used_count"], 2)
        record = restarted.create(self.underwriter, "RI-30003",
                                  {**BASE_DATA, 'loss_amount': 1000000.0, 'aggregate_prior': 800000.0})
        record = restarted.act(self.underwriter, record["id"], record["version"], "bind",
                               {"underwriter_id": "UW-1"})
        with self.assertRaises(ReinstatementExhausted):
            restarted.act(self.claims, record["id"], record["version"], "submit_claim",
                          {"claim_number": "CLM-3", "event_id": BASE_DATA['event_id']})

    def test_inconsistent_count_for_same_event_rejected(self):
        self._create_claim("RI-30001")
        data = dict(BASE_DATA)
        data['reinstatement_count'] = 3
        with self.assertRaises(Conflict) as caught:
            self.service.create(self.underwriter, "RI-30002", data)
        self.assertEqual(caught.exception.details["registered_count"], 2)
        self.assertEqual(caught.exception.details["submitted_count"], 3)

    def test_claim_event_must_match_contract_event(self):
        record = self._create_claim("RI-30001")
        record = self._bind(record)
        with self.assertRaises(ValidationError):
            self._submit(record, "CLM-X", event_id="TY-OTHER")

    def test_reinstatement_count_required_as_non_negative_integer(self):
        for reference, override in (
            ("RI-30010", "MISSING"),
            ("RI-30011", -1),
            ("RI-30012", 1.5),
        ):
            bad = dict(BASE_DATA)
            if override == "MISSING":
                del bad["reinstatement_count"]
            else:
                bad["reinstatement_count"] = override
            with self.assertRaises(ValidationError):
                self.service.create(self.underwriter, reference, bad)


if __name__ == "__main__":
    unittest.main()
