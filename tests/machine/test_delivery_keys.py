"""A file is delivered once, by artifact id, whether it went out mid-task or at the end."""

from __future__ import annotations

import pytest

from mavis.channels import progress_card
from mavis.domain.events import Event, EventType, Trust
from mavis.initiative import task_delivery
from mavis.store.db import utcnow
from mavis.store.repo import tasks
from tests.machine.fakes import RecordingCards


async def _art(user, tid, tmp_path, name, size=10):
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return await tasks.add_artifact(tid, user.id, kind=p.suffix.lstrip("."), path=str(p),
                                    mime="application/x", size=size)


def _completed(user, tid, paths):
    return Event(id=f"task:{tid}:completed", user_id=user.id, type=EventType.TASK_COMPLETED,
                 occurred_at=utcnow(), source="agent", trust=Trust.SYSTEM,
                 payload={"task_id": tid, "messages": ["Here you go."], "artifacts": paths, "origin": "user"})


async def test_file_sent_mid_task_is_not_sent_again_at_the_end(db, user, sent, tmp_path):
    rec = RecordingCards()
    progress_card.set_cards(rec)
    tid = await tasks.create(user.id, goal="chart my spending")
    a1 = await _art(user, tid, tmp_path, "spend.png")
    assert await task_delivery.deliver_artifact_now(user.id, tid, a1) is True
    assert await task_delivery.deliver_artifact_now(user.id, tid, a1) is False
    a2 = await _art(user, tid, tmp_path, "spend.csv")
    await task_delivery.deliver_task_result(_completed(user, tid, [str(tmp_path / "spend.png")]))
    docs = [m for m in sent if m.document_path]
    assert [m.dedupe_key for m in docs] == [f"task:{tid}:art:{a1}", f"task:{tid}:art:{a2}"]
    assert ("file", tid, 1) in rec.calls


@pytest.mark.parametrize("mb", [51, 80, 200])
async def test_too_big_files_are_named_not_attached(db, user, sent, tmp_path, mb):
    tid = await tasks.create(user.id, goal="export a video")
    aid = await tasks.add_artifact(tid, user.id, kind="mp4", path=str(tmp_path / "clip.mp4"),
                                   mime="video/mp4", size=mb * 1024 * 1024)
    await task_delivery.deliver_artifact_now(user.id, tid, aid)
    assert not [m for m in sent if m.document_path]
    assert any("clip.mp4" in m.text and f"{mb} MB" in m.text for m in sent)


async def test_fail_names_files_already_sent(db, user, sent, tmp_path, rec_bus):
    from mavis.agents import orchestrator
    from mavis.domain.tasks import TaskStatus

    tid = await tasks.create(user.id, goal="summarise sales")
    await tasks.claim(tid, TaskStatus.QUEUED, TaskStatus.RUNNING)
    a = await _art(user, tid, tmp_path, "sales_chart.png")
    await task_delivery.deliver_artifact_now(user.id, tid, a)
    await _art(user, tid, tmp_path, "sales_table.xlsx")  # made but not yet sent
    await orchestrator._fail(tid, user.id, "something broke on my side")
    snag = [m for m in sent if m.text.startswith("Hit a snag")][-1].text
    assert "sales_chart.png" in snag and "sales_table.xlsx" in snag
    assert "\u2014" not in snag
    assert len([m for m in sent if m.document_path]) == 2


async def test_redeliver_skips_delivered_files(db, user, sent, tmp_path):
    from mavis.domain.tasks import TaskOrigin, TaskStatus
    from mavis.store.repo import tasks as t

    tid = await t.create(user.id, goal="g", origin=TaskOrigin.INITIATIVE)
    await t.claim(tid, TaskStatus.QUEUED, TaskStatus.DONE, result_text="done")
    a = await _art(user, tid, tmp_path, "notes.txt")
    await task_delivery.deliver_artifact_now(user.id, tid, a, proactive=True)
    before = len([m for m in sent if m.document_path])
    await task_delivery.redeliver(user.id, tid)
    assert len([m for m in sent if m.document_path]) == before
