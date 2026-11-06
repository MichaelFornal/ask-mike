# ported from deal-terms-desk tests/test_limits.py on 2026-11-05, unchanged but for the import path
import threading

import pytest

from askmike.service.limits import Buckets, Slots, client_key


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_ipv4_is_its_own_key():
    assert client_key("203.0.113.7") == "203.0.113.7"


def test_ipv6_is_keyed_by_its_slash_64():
    a, b = client_key("2001:db8:1:2:3:4:5:6"), client_key("2001:db8:1:2:ffff::1")
    assert a == b == "2001:db8:1:2::/64"
    assert client_key("2001:db8:1:3::1") != a


def test_ipv4_mapped_ipv6_is_the_ipv4_address():
    assert client_key("::ffff:203.0.113.7") == "203.0.113.7"


@pytest.mark.parametrize("host", ["testclient", "", "not-an-ip"])
def test_anything_else_is_used_as_is(host):
    assert client_key(host) == host


def test_a_bucket_allows_n_per_window_then_says_how_long_to_wait():
    clock = Clock()
    b = Buckets(3, 60, clock)
    assert [b.allow("a") for _ in range(3)] == [0.0, 0.0, 0.0]
    assert b.allow("a") == pytest.approx(60.0)
    assert b.allow("b") == 0.0  # keys are independent
    clock.t = 30.0
    assert b.allow("a") == pytest.approx(30.0)
    clock.t = 60.0  # the three calls made at t=0 have all left the window
    assert [b.allow("a") for _ in range(3)] == [0.0, 0.0, 0.0] and b.allow("a") == pytest.approx(60.0)


def test_a_bucket_is_exact_under_threads():
    b, got, start = Buckets(10, 60, Clock()), [], threading.Barrier(20)

    def go():
        start.wait()
        got.append(b.allow("a"))
    threads = [threading.Thread(target=go) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(w == 0.0 for w in got) == 10


def test_idle_keys_are_pruned():
    clock = Clock()
    b = Buckets(1, 60, clock)
    for k in range(2000):
        b.allow(f"k{k}")
    clock.t = 61.0
    for _ in range(1024):
        b.allow("live")
    assert len(b._log) < 10


def test_slots_fail_fast_when_full():
    s = Slots(2)
    assert s.try_acquire() and s.try_acquire() and not s.try_acquire()
    s.release()
    assert s.try_acquire()


def test_releasing_more_than_was_taken_is_an_error():
    s = Slots(1)
    with pytest.raises(ValueError):
        s.release()


def test_zero_is_a_closed_limit():
    b = Buckets(0, 60, Clock())
    assert b.allow("a") == pytest.approx(60.0) and b.allow("b") == pytest.approx(60.0)
    assert Slots(0).try_acquire() is False
    with pytest.raises(ValueError):
        Buckets(-1, 60)
