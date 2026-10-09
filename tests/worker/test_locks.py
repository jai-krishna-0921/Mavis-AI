

async def test_a_released_claim_can_be_won_again():
    from mavis.worker import locks

    assert await locks.claim("k-release", 600) is True
    assert await locks.claim("k-release", 600) is False
    await locks.release("k-release")
    assert await locks.claim("k-release", 600) is True
    await locks.release("k-release")
