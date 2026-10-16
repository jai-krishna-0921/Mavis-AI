"""A disconnect drops the Microsoft folder-id cache, so a reconnect to another mailbox never reuses it."""
import httpx

from .graphfake import FakeGraph, page
from .test_microsoft_mail import with_folders
from .test_microsoft_transport import U


def _g():
    return with_folders(FakeGraph()).on("GET", r"/v1.0/me/messages$", httpx.Response(200, json=page([])))


async def test_forget_user_drops_the_cached_folder_ids():
    g = _g()
    ex, _ = g.executor()
    await ex.execute(U, "mail.search", {"query": "invoice"})
    one = len(g.calls("GET", r"/me/mailFolders/"))
    await ex.execute(U, "mail.search", {"query": "invoice"})
    assert len(g.calls("GET", r"/me/mailFolders/")) == one          # cached for ten minutes
    ex.forget_user(U.user_id)
    await ex.execute(U, "mail.search", {"query": "invoice"})
    assert len(g.calls("GET", r"/me/mailFolders/")) == 2 * one      # fetched again for the next mailbox


async def test_forgetting_one_user_keeps_another_users_cache():
    g = _g()
    ex, _ = g.executor()
    await ex.execute(U, "mail.search", {"query": "invoice"})
    one = len(g.calls("GET", r"/me/mailFolders/"))
    ex.forget_user(U.user_id + 1)
    await ex.execute(U, "mail.search", {"query": "invoice"})
    assert len(g.calls("GET", r"/me/mailFolders/")) == one


async def test_router_disconnect_tells_the_executor_to_forget_the_user(tokens, oauth, client):
    from mavis.tools.integrations.native.router import NativeRouter

    from .test_router import FakeProvider
    from .test_router_merge import ms_grant

    class Ex:
        provider = __import__("mavis.tools.integrations.native.base", fromlist=["x"]).NativeProvider.MICROSOFT
        forgotten: list[int] = []

        def handles(self, action):
            return False

        def forget_user(self, uid):
            self.forgotten.append(uid)

    ex = Ex()
    router = NativeRouter(FakeProvider(), tokens, oauth, [ex], client)
    await ms_grant(tokens)
    await router.disconnect(U, "microsoft")
    assert ex.forgotten == [U.user_id]
