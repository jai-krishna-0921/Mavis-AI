from zento.memory.names import USER_KEY, is_user, node_key, normalize_name, sanitize_label, sanitize_rel


def test_normalize_name():
    assert normalize_name("  Jawahar,  R. ") == "jawahar r"
    assert normalize_name("O'Brien-Smith") == "o'brien-smith"


def test_node_key():
    assert node_key("Person", "Jawahar ") == "Person:jawahar"
    assert USER_KEY == "User:user"


def test_is_user():
    for n in ["User", "me", "I", "myself", " user "]:
        assert is_user(n)
    assert not is_user("Jawahar")


def test_sanitize_label_case_insensitive_and_fallback():
    assert sanitize_label("person") == "Person"
    assert sanitize_label("Organization") == "Organization"
    assert sanitize_label("Spaceship") == "Topic"


def test_sanitize_rel_vocab_and_injection():
    assert sanitize_rel("friend of") == "FRIEND_OF"
    assert sanitize_rel("works-at") == "WORKS_AT"
    assert sanitize_rel("LOVES") == "RELATED_TO"
    assert sanitize_rel("FRIEND_OF]->() DETACH DELETE n //") == "RELATED_TO"
