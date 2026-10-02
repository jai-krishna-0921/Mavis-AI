from zento.channels.fake import FakeChannel
from zento.domain.messages import Button


async def test_fake_channel_records_everything(tmp_path) -> None:
    ch = FakeChannel()
    ids = await ch.send_text(1, "hello", buttons=[[Button(label="OK", data="ok")]])
    await ch.send_typing(1)
    doc = tmp_path / "a.txt"
    doc.write_text("x")
    await ch.send_document(1, str(doc), caption="here")
    dest = await ch.download_file("f1", str(tmp_path / "dl.bin"))
    assert ids == [1]
    assert ch.texts == ["hello"]
    assert ch.sent[0].buttons[0][0].data == "ok"
    assert [s.kind for s in ch.sent] == ["text", "typing", "document"]
    assert (tmp_path / "dl.bin").read_bytes() == b"fake-file" and dest.endswith("dl.bin")


async def test_fake_channel_can_fail_on_demand() -> None:
    ch = FakeChannel()
    ch.fail_next.append(RuntimeError("down"))
    try:
        await ch.send_text(1, "x")
    except RuntimeError:
        pass
    assert ch.texts == []
    await ch.send_text(1, "y")
    assert ch.texts == ["y"]
