import asyncio
import re
import time

import pytest

from dev_link_mcp import patterns
from dev_link_mcp.patterns import PatternStuck

# About 2 s of backtracking with CPython's re on a typical machine; much longer on 34 a's.
RUNAWAY = re.compile(r"^(a+)+$")
RUNAWAY_TEXT = "a" * 34 + "!"


async def test_search_returns_the_matched_text():
    assert await patterns.search(re.compile(r"b+"), "aabbbc", timeout_s=10) == "bbb"
    assert await patterns.search(re.compile(r"z"), "aabbbc", timeout_s=10) is None
    assert await patterns.search(re.compile(r"ÉTÉ", re.IGNORECASE), "un été chaud", timeout_s=10) == "été"
    assert await patterns.search(re.compile(r"(?m)^ready$"), "boot\nready\n", timeout_s=10) == "ready"


async def test_subn_matches_re_subn():
    regex = re.compile(r"(\w+)@(\w+)")
    texts = ["a@b and c@d", "none here", "ü@ö\n"]
    assert await patterns.subn(regex, r"\2@\1", texts, literal=False, timeout_s=10) == [
        regex.subn(r"\2@\1", t) for t in texts
    ]
    # Literal text keeps backslashes and group references as they are.
    assert await patterns.subn(regex, r"\1\n", ["x@y"], literal=True, timeout_s=10) == [(r"\1\n", 1)]


async def test_subn_bad_template_raises_like_re():
    with pytest.raises(re.error):
        await patterns.subn(re.compile(r"(a)"), r"\2", ["a"], literal=False, timeout_s=10)


async def test_runaway_pattern_is_stopped_on_time_and_the_loop_keeps_running():
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    started = time.monotonic()
    try:
        with pytest.raises(PatternStuck, match=r"did not finish within 0\.5s and was stopped"):
            await patterns.search(RUNAWAY, RUNAWAY_TEXT, timeout_s=0.5)
    finally:
        ticker.cancel()
    assert time.monotonic() - started < 1.5
    # In-process, re holds the GIL for the whole match and the loop would not tick at all.
    assert ticks > 10
    # The killed worker is replaced on the next call.
    assert await patterns.search(re.compile("a!"), RUNAWAY_TEXT, timeout_s=10) == "a!"


async def test_work_queued_behind_a_runaway_pattern_keeps_its_own_deadline():
    runaway = asyncio.create_task(patterns.search(RUNAWAY, RUNAWAY_TEXT, timeout_s=3))
    await asyncio.sleep(0.1)
    started = time.monotonic()
    with pytest.raises(PatternStuck):
        await patterns.search(re.compile("x"), "x", timeout_s=0.3)
    assert time.monotonic() - started < 1
    with pytest.raises(PatternStuck):
        await runaway


async def test_concurrent_searches_all_complete():
    digits = "0123456789"
    results = await asyncio.gather(*(patterns.search(re.compile(d), digits, timeout_s=10) for d in digits))
    assert results == list(digits)


async def test_cancelled_runaway_pattern_frees_the_worker_at_once():
    runaway = asyncio.create_task(patterns.search(RUNAWAY, RUNAWAY_TEXT, timeout_s=60))
    await asyncio.sleep(0.2)
    runaway.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runaway
    started = time.monotonic()
    # Without abandoning the cancelled job, this would queue behind it for the full 60 s.
    assert await patterns.search(re.compile("x"), "x", timeout_s=5) == "x"
    assert time.monotonic() - started < 2


async def test_subn_returns_unchanged_texts_as_given():
    texts = ["nothing to see", "a b a"]
    assert await patterns.subn(re.compile("a"), "z", texts, literal=True, timeout_s=10) == [
        ("nothing to see", 0),
        ("z b z", 2),
    ]
