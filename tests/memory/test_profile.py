from zento.domain.memory import ProfileUpdate
from zento.memory.profile import MAX_ITEMS, ProfileCard
from zento.memory.tokens import estimate_tokens
from zento.store.repo import profile as profile_repo


def test_apply_maps_fields_and_dedupes():
    card = ProfileCard().apply([
        ProfileUpdate(field="name", value="Jai"),
        ProfileUpdate(field="goals", value="Land a job"),
        ProfileUpdate(field="goals", value="land a job "),
        ProfileUpdate(field="key_people", value="Jawahar (friend)"),
        ProfileUpdate(field="weird", value="Night owl"),
        ProfileUpdate(field="timezone", value="Not/AZone"),
        ProfileUpdate(field="timezone", value="Asia/Kolkata"),
    ])
    assert card.name == "Jai" and card.timezone == "Asia/Kolkata"
    assert card.goals == ["Land a job"]
    assert card.other == ["Night owl"]


def test_list_fields_keep_latest_items():
    card = ProfileCard().apply([ProfileUpdate(field="routines", value=f"r{i}") for i in range(12)])
    assert card.routines == [f"r{i}" for i in range(12 - MAX_ITEMS, 12)]


def test_render_respects_token_budget():
    card = ProfileCard(name="Jai", goals=["g" * 300] * 8, other=["o" * 300] * 8)
    text = card.render()
    assert estimate_tokens(text) <= 400
    assert "Name: Jai" in text


def test_mood_flag_default_and_off():
    assert ProfileCard().tracks_mood
    assert not ProfileCard(flags={"track_mood": False}).tracks_mood


def test_remove_matching():
    card = ProfileCard(key_people=["Jawahar (friend)", "Amma"], goals=["Learn Teamcenter"])
    new, changed = card.remove_matching("jawahar")
    assert changed and new.key_people == ["Amma"]
    _, unchanged = new.remove_matching("zzz")
    assert not unchanged


async def test_repo_versions(db):
    assert (await profile_repo.get(1)).version == 0
    v1 = await profile_repo.save(1, ProfileCard(name="Jai"))
    current = await profile_repo.get(1)
    v2 = await profile_repo.save(1, current.apply([ProfileUpdate(field="tone", value="casual")]))
    assert (v1, v2) == (1, 2)
    latest = await profile_repo.get(1)
    assert latest.version == 2 and latest.name == "Jai" and latest.tone == "casual"
    assert [c.version for c in await profile_repo.history(1)] == [2, 1]
