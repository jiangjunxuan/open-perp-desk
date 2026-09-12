import unittest
from datetime import datetime, timedelta, timezone

from app.risk_engine import RiskEngine, RiskLimits
from app.trading_signal import TradeSignal


def valid_signal(**overrides: object) -> TradeSignal:
    values: dict[str, object] = {
        "inst_id": "BTC-USDT-SWAP",
        "action": "open_long",
        "confidence": 0.8,
        "leverage": 2,
        "position_pct": 5,
        "entry_price": 70000,
        "stop_loss": 68000,
        "take_profit": 74000,
        "source": "test",
    }
    values.update(overrides)
    return TradeSignal(**values)


class TradeSignalTests(unittest.TestCase):
    def test_long_signal_requires_ordered_prices(self) -> None:
        with self.assertRaises(ValueError):
            valid_signal(stop_loss=71000)

    def test_expired_signal_is_rejected_at_model_boundary(self) -> None:
        now = datetime.now(timezone.utc)
        with self.assertRaises(ValueError):
            valid_signal(created_at=now, expires_at=now - timedelta(seconds=1))


class RiskEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = RiskEngine(
            RiskLimits(
                max_leverage=3,
                max_position_pct=10,
                min_confidence=0.65,
                max_daily_loss_pct=3,
                max_stop_distance_pct=5,
            )
        )

    def test_valid_signal_is_approved_for_evaluation(self) -> None:
        decision = self.engine.evaluate(
            valid_signal(),
            account_equity=1000,
            daily_pnl_pct=0,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.reasons, ())

    def test_stale_market_data_blocks_evaluation(self) -> None:
        signal = TradeSignal(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            confidence=0.9,
            leverage=2,
            position_pct=5,
            entry_price=100,
            stop_loss=95,
            take_profit=110,
        )
        decision = RiskEngine().evaluate(
            signal,
            account_equity=1000,
            daily_pnl_pct=0,
            market_data_fresh=False,
        )
        self.assertFalse(decision.approved)
        self.assertIn("market_data_stale", decision.reasons)

    def test_risk_limits_accumulate_rejection_reasons(self) -> None:
        decision = self.engine.evaluate(
            valid_signal(confidence=0.4, leverage=5, position_pct=20),
            account_equity=1000,
            daily_pnl_pct=-4,
        )
        self.assertFalse(decision.approved)
        self.assertIn("confidence_below_threshold", decision.reasons)
        self.assertIn("leverage_above_limit", decision.reasons)
        self.assertIn("position_size_above_limit", decision.reasons)
        self.assertIn("daily_loss_limit_reached", decision.reasons)

    def test_total_exposure_limit_includes_new_order_notional(self) -> None:
        decision = self.engine.evaluate(
            valid_signal(position_pct=5, leverage=2),
            account_equity=1000,
            daily_pnl_pct=0,
            current_notional=250,
        )
        self.assertFalse(decision.approved)
        self.assertIn("total_exposure_above_limit", decision.reasons)

    def test_hold_is_never_an_opening_signal(self) -> None:
        decision = self.engine.evaluate(
            TradeSignal(
                inst_id="BTC-USDT-SWAP",
                action="hold",
                confidence=0.9,
                leverage=1,
                position_pct=0,
                source="test",
            ),
            account_equity=1000,
            daily_pnl_pct=0,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reasons, ("hold_signal",))

    def test_expired_signal_is_rejected_by_risk_engine(self) -> None:
        now = datetime.now(timezone.utc)
        signal = valid_signal(
            created_at=now - timedelta(minutes=2),
            expires_at=now - timedelta(seconds=1),
        )
        decision = self.engine.evaluate(
            signal,
            account_equity=1000,
            daily_pnl_pct=0,
        )
        self.assertFalse(decision.approved)
        self.assertIn("signal_expired", decision.reasons)


if __name__ == "__main__":
    unittest.main()
