"""重试退避与 429 降速测试。

关键约束：**不真等 30 秒**。时间源与 sleep 都可注入，
测试用假时钟断言降速曲线，跑完整个用例应在毫秒级。
"""

from __future__ import annotations

import asyncio
import random

import pytest

from datapipe.fetch import RateLimiter, backoff_delays


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# -- 指数退避序列 ----------------------------------------------------------
def test_backoff_delays_exponential() -> None:
    assert backoff_delays(3, base_delay=1.0, factor=2.0, jitter_ratio=0.0) == [1.0, 2.0, 4.0]


def test_backoff_delays_custom_base_and_factor() -> None:
    assert backoff_delays(4, base_delay=0.5, factor=3.0, jitter_ratio=0.0) == [
        0.5,
        1.5,
        4.5,
        13.5,
    ]


def test_backoff_delays_zero_retries() -> None:
    assert backoff_delays(0) == []


def test_backoff_delays_single_retry() -> None:
    assert backoff_delays(1, base_delay=2.0, factor=2.0, jitter_ratio=0.0) == [2.0]


def test_backoff_delays_rejects_invalid_base() -> None:
    with pytest.raises(ValueError, match="base_delay"):
        backoff_delays(3, base_delay=0)


def test_backoff_delays_rejects_shrinking_factor() -> None:
    """factor < 1 会让重试间隔越来越短，等于变相加压，必须拒绝。"""
    with pytest.raises(ValueError, match="factor"):
        backoff_delays(3, factor=0.5)


def test_backoff_delays_jitter_within_bounds() -> None:
    rng = random.Random(42)
    delays = backoff_delays(5, base_delay=1.0, factor=2.0, jitter_ratio=0.1, rng=rng)

    for index, delay in enumerate(delays):
        nominal = 1.0 * (2.0**index)
        assert nominal * 0.9 <= delay <= nominal * 1.1


def test_backoff_delays_jitter_is_deterministic_with_seed() -> None:
    first = backoff_delays(3, jitter_ratio=0.2, rng=random.Random(7))
    second = backoff_delays(3, jitter_ratio=0.2, rng=random.Random(7))

    assert first == second


# -- 429 降速 --------------------------------------------------------------
def test_rate_limiter_halves_limit_on_429() -> None:
    limiter = RateLimiter(global_limit=8, per_host_limit=2, cooldown_s=30.0)

    assert limiter.effective_limit == 8
    assert limiter.on_rate_limited() == (4, 30.0)
    assert limiter.effective_limit == 4


def test_rate_limiter_degrade_sequence() -> None:
    """连续命中 429 时额度应按 8→4→2→1→1 收缩，且不会归零。"""
    limiter = RateLimiter(global_limit=8, per_host_limit=2, cooldown_s=30.0)

    sequence = [limiter.effective_limit]
    for _ in range(4):
        limiter.on_rate_limited()
        sequence.append(limiter.effective_limit)

    assert sequence == [8, 4, 2, 1, 1]


def test_rate_limiter_records_degrade_events() -> None:
    limiter = RateLimiter(global_limit=8, per_host_limit=2, cooldown_s=30.0)
    limiter.on_rate_limited()
    limiter.on_rate_limited()

    events = limiter.degrade_events
    assert len(events) == 2
    assert (events[0].limit_before, events[0].limit_after) == (8, 4)
    assert (events[1].limit_before, events[1].limit_after) == (4, 2)


def test_rate_limiter_cooldown_blocks_recovery() -> None:
    clock = FakeClock()
    limiter = RateLimiter(global_limit=8, per_host_limit=2, cooldown_s=30.0, clock=clock)
    limiter.on_rate_limited()

    clock.advance(10)
    assert limiter.in_cooldown is True
    # 冷却期内调用 recover 不应提额
    assert limiter.recover() == 4


def test_rate_limiter_recovers_after_cooldown() -> None:
    clock = FakeClock()
    limiter = RateLimiter(global_limit=8, per_host_limit=2, cooldown_s=30.0, clock=clock)
    limiter.on_rate_limited()

    clock.advance(31)
    assert limiter.in_cooldown is False
    assert limiter.recover() == 8


def test_rate_limiter_recovery_is_gradual() -> None:
    """恢复应逐级翻倍（4→8），而不是一次跳满，避免刚放行就再次冲击对方。"""
    clock = FakeClock()
    limiter = RateLimiter(global_limit=8, per_host_limit=2, cooldown_s=30.0, clock=clock)
    limiter.on_rate_limited()
    limiter.on_rate_limited()  # 8 -> 4 -> 2

    clock.advance(31)
    assert limiter.recover() == 4

    clock.advance(31)
    assert limiter.recover() == 8

    clock.advance(31)
    assert limiter.recover() == 8  # 不超初始上限


def test_rate_limiter_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError, match="global_limit"):
        RateLimiter(global_limit=0)
    with pytest.raises(ValueError, match="per_host_limit"):
        RateLimiter(global_limit=4, per_host_limit=8)


def test_rate_limiter_from_config(app_config) -> None:
    limiter = RateLimiter.from_config(app_config.http.concurrency)

    assert limiter.effective_limit == app_config.http.concurrency.global_limit


# -- 闸门行为（无冷却，故用真时钟不会阻塞）--------------------------------
def test_gate_acquires_and_releases() -> None:
    limiter = RateLimiter(global_limit=2, per_host_limit=2, cooldown_s=0.0)

    async def scenario() -> int:
        async with limiter.gate("example.test"):
            inside = limiter.active
        after = limiter.active
        return inside * 10 + after

    assert asyncio.run(scenario()) == 10  # 进入时 1 个活跃，退出后 0


def test_gate_limits_concurrency() -> None:
    """全局上限 2：同时最多 2 个任务在闸门内。"""
    limiter = RateLimiter(global_limit=2, per_host_limit=2, cooldown_s=0.0)
    peak = 0

    async def worker() -> None:
        nonlocal peak
        async with limiter.gate("example.test"):
            peak = max(peak, limiter.active)
            await asyncio.sleep(0.01)

    async def scenario() -> None:
        await asyncio.gather(*(worker() for _ in range(6)))

    asyncio.run(scenario())
    assert peak == 2
