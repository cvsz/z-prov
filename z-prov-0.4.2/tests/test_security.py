from z_prov.security import InMemoryRateLimiter, redact, request_id


def test_redaction_and_request_id_validation():
    assert redact("token sk-secretsecretsecret value") == "token [REDACTED] value"
    assert request_id("safe-id.1") == "safe-id.1"
    generated = request_id("../../bad")
    assert len(generated) == 32
    assert generated.isalnum()


def test_rate_limiter_window_is_deterministic():
    now = [0.0]
    limiter = InMemoryRateLimiter(2, 60, clock=lambda: now[0])
    assert limiter.allow("client") == (True, 1)
    assert limiter.allow("client") == (True, 0)
    assert limiter.allow("client") == (False, 0)
    now[0] = 61
    assert limiter.allow("client") == (True, 1)


def test_rate_limiter_evicts_buckets_that_go_permanently_idle():
    # A client seen once and never again should not leak a dict entry
    # forever -- it should be swept out after its events age past the
    # window, even though nothing ever calls allow("one_shot_client")
    # again to trigger the old bucket-local eviction path.
    now = [0.0]
    limiter = InMemoryRateLimiter(5, 60, clock=lambda: now[0])
    limiter.allow("one_shot_client")
    assert "one_shot_client" in limiter._events

    # Some other client keeps the limiter "alive" (making calls) well past
    # one_shot_client's window, which is what triggers the periodic sweep.
    now[0] = 61.0
    limiter.allow("other_client")
    assert "one_shot_client" not in limiter._events
    assert "other_client" in limiter._events


def test_rate_limiter_does_not_evict_active_buckets():
    now = [0.0]
    limiter = InMemoryRateLimiter(5, 60, clock=lambda: now[0])
    limiter.allow("steady_client")
    now[0] = 30.0
    limiter.allow("steady_client")  # still within window, keeps bucket alive
    now[0] = 61.0
    limiter.allow("other_client")  # triggers a sweep
    # steady_client's most recent event (t=30) is still within the 60s
    # window measured from t=61, so it must not be evicted.
    assert "steady_client" in limiter._events
