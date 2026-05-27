from app.services.quiet_hours import is_quiet_now


def test_runs_without_error():
    # Just smoke-test; real logic is timezone-dependent.
    assert isinstance(is_quiet_now(), bool)
