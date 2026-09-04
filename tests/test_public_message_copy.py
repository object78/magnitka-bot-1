from pathlib import Path


def test_public_signal_templates_hide_strategy_names():
    src = (Path(__file__).parents[1] / "app" / "monitor.py").read_text(encoding="utf-8")
    forbidden = [
        'f"🔴 M3 —',
        'f"🟡 A+ —',
        'f"🟡 IT-L2 —',
        'f"🟠 A+ —',
        'f"🟠 IT-L2 —',
        'f"🚨 A+ —',
        'f"🚨 IT-L2 —',
        'f"⚪ A+ —',
        'f"⚪ IT-L2 —',
        'f"База: {cand.base_reason}',
    ]
    for item in forbidden:
        assert item not in src
