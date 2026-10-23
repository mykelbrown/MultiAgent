from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from aegis.core.audit import AuditLog
from aegis.core.config import RiskLimits
from aegis.core.permissions import ApprovalStore, Capability, PermissionDenied, PermissionGate
from aegis.core.redaction import REDACTED, redact, redact_text


def test_audit_chain_verifies_and_detects_tampering(tmp_path):
    path = tmp_path / "a.db"
    log = AuditLog(path)
    for i in range(5):
        log.append("tester", "event", {"i": i})
    assert log.verify() == (True, "ok (5 records)")
    log.close()

    # Triggers block UPDATE/DELETE through the normal API...
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit SET payload='{}' WHERE seq=2")
    # ...and if an attacker drops the triggers, the hash chain still catches the edit.
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("""UPDATE audit SET payload='{"i": 99}' WHERE seq=2""")
    conn.commit()
    conn.close()
    ok, why = AuditLog(path).verify()
    assert not ok and "seq 2" in why


def test_audit_detects_deletion(tmp_path):
    path = tmp_path / "a.db"
    log = AuditLog(path)
    for i in range(3):
        log.append("t", "e", {"i": i})
    log.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER audit_no_delete")
    conn.execute("DELETE FROM audit WHERE seq=2")
    conn.commit()
    conn.close()
    assert AuditLog(path).verify()[0] is False


def test_audit_redacts_secrets():
    log = AuditLog()
    rec = log.append("t", "e", {"api_key": "abc", "note": "token 123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA end"})
    assert rec.payload["api_key"] == REDACTED
    assert "AAAA" not in rec.payload["note"]


@pytest.mark.parametrize(
    "text",
    [
        "key sk-ant-REDACTEDREDACTEDREDACTED123",
        "ghp_" + "a" * 36,
        "AKIAABCDEFGHIJKLMNOP",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_redact_text_patterns(text):
    assert REDACTED in redact_text(text)


def test_redact_nested_keys():
    out = redact({"outer": [{"Password": "x", "ok": "fine"}], "session_cookie": "y"})
    assert out == {"outer": [{"Password": REDACTED, "ok": "fine"}], "session_cookie": REDACTED}


def test_gate_denies_non_allowlisted():
    audit = AuditLog()
    gate = PermissionGate(audit, ApprovalStore(audit=audit))
    with pytest.raises(PermissionDenied):
        gate.check("assistant", Capability.PLACE_PAPER_ORDER)
    with pytest.raises(PermissionDenied):
        gate.check("unknown_agent", Capability.DISCOVERY_READ)
    gate.check("discovery", Capability.DISCOVERY_READ)
    assert [r.action for r in audit.records()][-1] == "permission.granted"


def test_no_agent_can_move_funds_or_merge_even_with_approval():
    audit = AuditLog()
    approvals = ApprovalStore(audit=audit)
    gate = PermissionGate(audit, approvals)
    approvals.grant(Capability.MOVE_FUNDS, "owner")
    approvals.grant(Capability.MERGE_TO_MAIN, "owner")
    for agent in ("assistant", "agent_factory", "telegram_intake", "forex_research"):
        with pytest.raises(PermissionDenied):
            gate.check(agent, Capability.MOVE_FUNDS)
        with pytest.raises(PermissionDenied):
            gate.check(agent, Capability.MERGE_TO_MAIN)


def test_approval_required_is_single_use_and_scoped():
    audit = AuditLog()
    approvals = ApprovalStore(audit=audit)
    gate = PermissionGate(audit, approvals)
    with pytest.raises(PermissionDenied, match="approval"):
        gate.check("assistant", Capability.SEND_EXTERNAL_MESSAGE, {"to": "a@b.c"})
    approvals.grant(Capability.SEND_EXTERNAL_MESSAGE, "owner", scope={"to": "a@b.c"})
    with pytest.raises(PermissionDenied):
        gate.check("assistant", Capability.SEND_EXTERNAL_MESSAGE, {"to": "evil@x.y"})  # scope mismatch
    gate.check("assistant", Capability.SEND_EXTERNAL_MESSAGE, {"to": "a@b.c"})
    with pytest.raises(PermissionDenied):
        gate.check("assistant", Capability.SEND_EXTERNAL_MESSAGE, {"to": "a@b.c"})  # consumed


def test_approval_expires():
    approvals = ApprovalStore()
    approvals.grant(Capability.INSTALL_TOOL, "owner", ttl=timedelta(seconds=-1))
    assert approvals.consume(Capability.INSTALL_TOOL) is None


def test_approvals_persist(tmp_path):
    p = tmp_path / "ap.json"
    ApprovalStore(p).grant(Capability.RESET_KILL_SWITCH, "owner")
    assert ApprovalStore(p).consume(Capability.RESET_KILL_SWITCH) is not None
    assert ApprovalStore(p).consume(Capability.RESET_KILL_SWITCH) is None


def test_risk_limit_increase_detection():
    base = RiskLimits()
    assert base.model_copy(update={"risk_per_trade_pct": 0.25}).is_increase_over(base) == []
    assert base.model_copy(update={"risk_per_trade_pct": 1.0}).is_increase_over(base) == ["risk_per_trade_pct"]
    assert base.model_copy(update={"min_stop_distance_pips": 1.0}).is_increase_over(base) == ["min_stop_distance_pips"]


def test_risk_limits_have_hard_ceilings():
    with pytest.raises(ValueError):
        RiskLimits(risk_per_trade_pct=5)
    with pytest.raises(ValueError):
        RiskLimits(daily_loss_limit_pct=50)
