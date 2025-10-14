from tools.auto.capability_negotiator import filter_suites_for_follower


def test_filter_suites_all_supported():
    suites = ["a", "b", "c"]
    caps = {"supported_suites": suites}
    filtered, skips = filter_suites_for_follower(suites, caps)
    assert filtered == suites
    assert skips == []


def test_filter_suites_partial():
    suites = ["a", "b", "c"]
    caps = {"supported_suites": ["a", "c"]}
    filtered, skips = filter_suites_for_follower(suites, caps)
    assert filtered == ["a", "c"]
    assert any(s["suite"] == "b" for s in skips)
