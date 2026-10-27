"""scripts/export_game_batch.py and scripts/import_game_consensus.py: the
two ends of the crowd-review game's file contract (plans/knock-game.md)."""

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from knock.core.audit import AuditEntry, JSONLAuditLog
from knock.core.scene import SceneContext, SceneObservation
from knock.core.training import (
    TrainingMetadata,
    TrainingMetadataStore,
    TrainingReview,
    TrainingReviewStore,
    example_key,
)
from knock.core.training_judge import AggregatedJudgeResult

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


export = _load("export_game_batch")
importer = _load("import_game_consensus")

_T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _entry(n: int, text: str, reply: str, intent: str | None = "delivery", **kw) -> AuditEntry:
    return AuditEntry(
        timestamp=_T0 + timedelta(seconds=n),
        text=text,
        response_text=reply,
        allowed=True,
        reason="llm",
        intent=intent,
        session_id="sess-secret",
        **kw,
    )


def _write_log(tmp_path: Path, entries: list[AuditEntry], *, in_manifest=None) -> Path:
    log = JSONLAuditLog(tmp_path / "synthetic.jsonl")
    rows = []
    for entry in entries:
        log.record(entry)
        if in_manifest is None or entry in in_manifest:
            rows.append(
                {
                    "model": "m",
                    "scenario": entry.text,
                    "intent": entry.intent,
                    "reason": entry.reason,
                    "timestamp": entry.timestamp.isoformat(),
                    "response_text": entry.response_text,
                }
            )
    manifest = tmp_path / "synthetic.manifest.jsonl"
    manifest.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return log.path


def _judge(**kw) -> AggregatedJudgeResult:
    base = dict(
        visitor_voice_avg=9.0,
        category_correct_avg=8.0,
        safety_compliant_avg=10.0,
        natural_quality_avg=7.0,
        voice_veto=False,
        safety_veto=False,
        disagreement=0.5,
    )
    base.update(kw)
    return AggregatedJudgeResult(**base)


def _run_export(tmp_path, monkeypatch, *extra: str) -> tuple[int, dict]:
    out = tmp_path / "batch.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "export_game_batch.py",
            "--audit-log",
            str(tmp_path / "synthetic.jsonl"),
            "--out",
            str(out),
            "--reviews",
            str(tmp_path / "reviews.json"),
            "--metadata-store",
            str(tmp_path / "meta.json"),
            *extra,
        ],
    )
    code = export.main()
    return code, (json.loads(out.read_text()) if out.exists() else {})


# -- export: the synthetic-only gate ----------------------------------------


def test_refuses_the_production_audit_log(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(export, "PRODUCTION_AUDIT_LOG", tmp_path / "audit.jsonl")
    with pytest.raises(export.ExportRefused, match="production"):
        export.synthetic_keys(tmp_path / "audit.jsonl")


def test_refuses_a_log_with_no_manifest(tmp_path, monkeypatch) -> None:
    JSONLAuditLog(tmp_path / "synthetic.jsonl").record(_entry(1, "hi", "hello"))
    code, batch = _run_export(tmp_path, monkeypatch)
    assert code == 2
    assert batch == {}


def test_entries_missing_from_the_manifest_are_never_exported(tmp_path, monkeypatch) -> None:
    marked = _entry(1, "Package for you", "Thanks, leave it by the door.")
    stray = _entry(2, "Real visitor line", "Real reply")
    _write_log(tmp_path, [marked, stray], in_manifest=[marked])

    code, batch = _run_export(tmp_path, monkeypatch)

    assert code == 0
    texts = {item["text"] for item in batch["items"]}
    assert texts == {"Package for you"}


def test_no_metadata_leaks_into_the_batch(tmp_path, monkeypatch) -> None:
    scene = SceneContext(
        camera="Front Door G4",
        zones=["porch"],
        captured_at=_T0,
        observation=SceneObservation(summary="A person holding a box", visible_text="FRAGILE"),
    )
    _write_log(tmp_path, [_entry(1, "Package for you", "Thanks!", scene=scene)])

    _, batch = _run_export(tmp_path, monkeypatch)

    dumped = json.dumps(batch)
    for secret in ("Front Door G4", "porch", "sess-secret", "2026-10-01"):
        assert secret not in dumped
    [item] = batch["items"]
    assert item["scene"] == "A person holding a box; visible text: 'FRAGILE'"


# -- export: item building ---------------------------------------------------


def test_builds_intent_and_pair_items_with_gold_from_human_reviews(tmp_path, monkeypatch) -> None:
    good = _entry(1, "Package for you", "Thanks, please leave it on the porch.")
    bad = _entry(2, "Package for you", "Nobody's home, just leave it.")
    _write_log(tmp_path, [good, bad])
    reviews = TrainingReviewStore(tmp_path / "reviews.json")
    reviews.set(example_key(good), TrainingReview(status="approved", intent_override="delivery"))
    reviews.set(example_key(bad), TrainingReview(status="rejected"))
    TrainingMetadataStore(tmp_path / "meta.json").set(
        example_key(good), TrainingMetadata(judge=_judge())
    )

    _, batch = _run_export(tmp_path, monkeypatch, "--batch", "b1")

    assert batch["format"] == "knock-game-batch"
    assert batch["version"] == 1
    assert batch["batch"] == "b1"
    assert "delivery" in batch["intents"] and "unknown" in batch["intents"]
    intents = [i for i in batch["items"] if i["mode"] == "intent"]
    pairs = [i for i in batch["items"] if i["mode"] == "pair"]
    # The rejected entry is a fine losing reply, but not an intent item.
    assert [i["knock_key"] for i in intents] == [example_key(good)]
    assert intents[0]["gold_answer"] == "delivery"
    assert intents[0]["judge"]["category_correct"] == 8.0
    [pair] = pairs
    assert pair["gold_answer"] == example_key(good)
    assert {r["knock_key"] for r in pair["replies"]} == {example_key(good), example_key(bad)}
    assert (
        pair["knock_key"]
        == f"pair:{pair['replies'][0]['knock_key']}:{pair['replies'][1]['knock_key']}"
    )


def test_skips_vetoed_unclassified_and_filter_replaced_entries(tmp_path, monkeypatch) -> None:
    resident = _entry(1, "We're not interested, thanks", "Okay!")
    blocked = _entry(2, "Is anyone home?", "I can't help with that.", intent=None)
    replaced = _entry(3, "Pizza!", "Please leave it at the door.", replaced_by="safety_check")
    other = _entry(4, "Pizza!", "Thanks, leave it on the bench.")
    _write_log(tmp_path, [resident, blocked, replaced, other])
    TrainingMetadataStore(tmp_path / "meta.json").set(
        example_key(resident), TrainingMetadata(judge=_judge(voice_veto=True))
    )

    _, batch = _run_export(tmp_path, monkeypatch)

    keys = {i["knock_key"] for i in batch["items"]}
    assert keys == {example_key(replaced), example_key(other)}  # no pair: one real reply


def test_caps_pairs_per_visitor_line(tmp_path, monkeypatch) -> None:
    _write_log(tmp_path, [_entry(n, "Package", f"reply {n}") for n in range(5)])

    _, batch = _run_export(tmp_path, monkeypatch, "--max-pairs-per-line", "2")

    assert sum(1 for i in batch["items"] if i["mode"] == "pair") == 2


# -- import ------------------------------------------------------------------


def _consensus(*items: dict) -> object:
    return importer.ConsensusFile.model_validate(
        {"format": "knock-game-consensus", "version": 1, "day": "2026-10-27", "items": items}
    )


def _item(key: str, mode="intent", status="settled", label=None) -> dict:
    return {
        "knock_key": key,
        "mode": mode,
        "status": status,
        "label": label,
        "share": 0.9,
        "total_weight": 6.2,
    }


def test_settled_intent_is_approved_with_an_override_only_on_disagreement() -> None:
    agree = _entry(1, "Package", "Thanks")
    differ = _entry(2, "Got your food", "Thanks")
    entries = {example_key(e): e for e in (agree, differ)}

    planned, skipped = importer.plan_reviews(
        _consensus(
            _item(example_key(agree), label="delivery"),
            _item(example_key(differ), label="food_delivery"),
        ),
        entries,
        {},
    )

    assert skipped == []
    assert planned[example_key(agree)].status == "approved"
    assert planned[example_key(agree)].intent_override is None
    assert planned[example_key(differ)].intent_override == "food_delivery"
    assert "90% of weight 6.2" in (planned[example_key(differ)].comment or "")


def test_disputed_and_both_bad_stay_pending_for_a_human() -> None:
    a, b, c = (_entry(n, "Package", f"r{n}") for n in range(3))
    entries = {example_key(e): e for e in (a, b, c)}
    pair = f"pair:{example_key(a)}:{example_key(b)}"

    planned, _ = importer.plan_reviews(
        _consensus(
            _item(pair, mode="pair", label="both_bad"),
            _item(example_key(c), status="disputed", label="delivery"),
        ),
        entries,
        {},
    )

    assert {k: r.status for k, r in planned.items()} == {
        example_key(a): "pending",
        example_key(b): "pending",
        example_key(c): "pending",
    }


def test_settled_pair_approves_only_the_winner() -> None:
    a, b = _entry(1, "Package", "good"), _entry(2, "Package", "bad")
    entries = {example_key(e): e for e in (a, b)}
    pair = f"pair:{example_key(a)}:{example_key(b)}"

    planned, _ = importer.plan_reviews(
        _consensus(_item(pair, mode="pair", label=example_key(a))), entries, {}
    )

    assert list(planned) == [example_key(a)]
    assert planned[example_key(a)].status == "approved"


def test_never_overwrites_a_human_or_invents_keys() -> None:
    mine = _entry(1, "Package", "Thanks")
    entries = {example_key(mine): mine}

    planned, skipped = importer.plan_reviews(
        _consensus(
            _item(example_key(mine), label="soliciting"),
            _item("0000000000000000", label="delivery"),
            _item(example_key(mine), label="not_an_intent"),
        ),
        entries,
        {example_key(mine): TrainingReview(status="rejected")},
    )

    assert planned == {}
    assert len(skipped) == 3


def test_import_main_writes_reviews(tmp_path, monkeypatch) -> None:
    entry = _entry(1, "Package", "Thanks")
    log = _write_log(tmp_path, [entry])
    path = tmp_path / "consensus-2026-10-27.json"
    path.write_text(
        json.dumps(
            {
                "format": "knock-game-consensus",
                "version": 1,
                "day": "2026-10-27",
                "items": [_item(example_key(entry), label="delivery")],
            }
        )
    )
    reviews = tmp_path / "reviews.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_game_consensus.py", str(path), "--audit-log", str(log), "--reviews", str(reviews)],
    )

    assert importer.main() == 0
    assert TrainingReviewStore(reviews).all()[example_key(entry)].status == "approved"
