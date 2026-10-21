from datetime import date
from decimal import Decimal

from conftest import click, texts

from studio_bot.payroll import Entry, Snapshot, report_csv, report_text


def with_former(snapshot):
    return Snapshot(
        snapshot.entries
        + (
            Entry(
                "former",
                date(2026, 9, 16),
                "former",
                "Бывшая с процентом",
                Decimal("30"),
                Decimal("60"),
            ),
        ),
        snapshot.fetched_at,
    )


async def test_binding_uses_only_latest_month_and_rechecks_month_change(ui):
    snap = with_former(ui.payroll.get.return_value)
    ui.payroll.get.return_value = snap
    with ui.store.db:
        ui.store.db.execute("UPDATE people SET payroll_id=NULL WHERE user_id=10")
    ui.store.register(30, "Новая", "", "")
    ui.auth.sessions[99] = ui.store.clock() + 3600
    await click(ui, "bindstart:30", 99, "admin")
    state = ui.pending[("admin", 99)][1]
    assert state["candidates"] == [("m10", "Аня")]
    await click(ui, f"bindpick:{state['token']}:0", 99, "admin")
    ui.payroll.get.return_value = Snapshot(
        snap.entries
        + (Entry("nov", date(2026, 11, 1), "new", "Ноябрьская", Decimal(0), Decimal(0)),),
        snap.fetched_at,
    )
    await click(ui, f"bindconfirm:{state['token']}", 99, "admin")
    assert ui.store.user(30)["payroll_id"] is None
    assert "Запись в таблице изменилась" in texts(ui, "admin")


def test_current_roster_filters_historical_report_and_csv_but_preserves_income(ui):
    snap = with_former(ui.payroll.get.return_value)
    period = (date(2026, 9, 1), date(2026, 9, 30))
    people = [{"payroll_id": "former", "name": "Старая привязка"}]
    text = report_text(snap, period, people, gross=True, active_only=True)
    csv = report_csv(snap, period, people, gross=True, active_only=True).decode("utf-8-sig")
    assert "Бывшая" not in text + csv and "Старая привязка" not in text + csv
    assert "Аня: $400.00" in text and "Всего: $400.00" in text
    assert snap.amount("m10", period) == Decimal("200")
    assert snap.amount("former", period, gross=True) == Decimal("60")
    assert "Бывшая с процентом" in report_text(snap, period, gross=True)


def test_latest_month_membership_overrides_old_inactive_flag(ui):
    old = ui.payroll.get.return_value
    snap = Snapshot(
        (Entry("old", date(2026, 9, 1), "m10", "Аня", None, Decimal(0), True),) + old.entries,
        old.fetched_at,
    )
    assert snap.names(active_only=True) == {"m10": "Аня", "m20": "Мария"}
