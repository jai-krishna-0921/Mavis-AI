# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
from mavis.tools.integrations.native import google_scopes, router


def test_router_reexports_action_scopes_and_nothing_private():
    assert router.ACTION_SCOPES is google_scopes.ACTION_SCOPES
    assert not hasattr(router, "_MAIL_READ") and not hasattr(router, "GOOGLE_FEATURES")
    assert "mail.search" in google_scopes.ACTION_SCOPES
