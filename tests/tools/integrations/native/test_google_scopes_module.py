# Copyright (c) 2026 Jai Krishna. All rights reserved.
# Proprietary. Use is governed by LICENSE in the repository root.
from mavis.tools.integrations.native import google_scopes, router


def test_router_reexports_the_same_objects():
    assert router.ACTION_SCOPES is google_scopes.ACTION_SCOPES
    assert router.GOOGLE_FEATURES is google_scopes.GOOGLE_FEATURES
    assert router._MAIL_READ is google_scopes._MAIL_READ
    assert "mail.search" in google_scopes.ACTION_SCOPES
