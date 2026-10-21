"""传输层：Fetcher 协议、限流器、指数退避重试、两种实现。

设计要点
--------
* :class:`Fetcher` 是窄协议，编排层只依赖 ``get`` / ``aclose``，
  因此"真实 HTTP"与"本地 fixture"可以无缝互换，测试完全离线。
* :class:`RateLimiter` 用"可变计数 + Condition"而不是 ``Semaphore``：
  只有前者能在收到 429 时把可用槽位*收缩*，并让等待者立刻重新评估。
* ``backoff_delays`` 与 ``RateLimiter`` 的时间源、sleep 都可注入，
  测试里用假时钟就能断言降速曲线，不必真的等 30 秒。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Protocol, runtime_checkable
from urllib.parse import urlparse

import aiohttp

from datapipe.config import ConcurrencyConfig, HttpConfig, RetryConfig
from datapipe.models import FetchResult

__all__ = [
    "FetchError",
    "Fetcher",
    "RateLimiter",
    "backoff_delays",
    "HttpFetcher",
    "FixtureFetcher",
    "build_fetcher",
]

logger = logging.getLogger(__name__)

#: 值得重试的 HTTP 状态码：429 限流、408 超时、425 过早、5xx 服务端错误
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class FetchError(Exception):
    """抓取失败（网络错误、非 2xx、重试耗尽）。"""

    def __init__(self, url: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{url} -> {message}")
        self.url = url
        self.message = message
        self.status = status


def backoff_delays(
    max_retries: int,
    base_delay: float = 1.0,
    factor: float = 2.0,
    jitter_ratio: float = 0.0,
    rng: random.Random | None = None,
) -> list[float]:
    """返回长度为 ``max_retries`` 的等待时长序列。

    第 ``n`` 次（从 0 计）重试等待 ``base_delay * factor ** n``，
    再叠加 ``±base*factor**n*jitter_ratio`` 的均匀抖动。
    抖动的作用是打散多个任务的重试节奏，避免它们再次同时打过去。

    >>> backoff_delays(3, 1.0, 2.0, 0.0)
    [1.0, 2.0, 4.0]
    """
    if max_retries <= 0:
        return []
    if base_delay <= 0:
        raise ValueError("base_delay 必须大于 0")
    if factor < 1.0:
        raise ValueError("factor 必须 >= 1.0")

    generator = rng if rng is not None else random.Random()
    delays: list[float] = []
    for attempt in range(max_retries):
        nominal = base_delay * (factor**attempt)
        if jitter_ratio > 0:
            spread = nominal * jitter_ratio
            nominal = max(0.0, nominal + generator.uniform(-spread, spread))
        delays.append(round(nominal, 6))
    return delays


@dataclass(frozen=True, slots=True)
class DegradeEvent:
    """一次 429 降速的记录，便于测试与日志核对。"""

    limit_before: int
    limit_after: int
    cooldown_s: float
    at: float


class RateLimiter:
    """全局 + 单域名双层闸门，并支持 429 触发的额度收缩。

    额度模型
    --------
    * ``effective_limit`` 是"当前允许的全局并发数"，初始为 ``global_limit``。
    * 命中 429 时立刻减半（下取整，最低 1），并进入 ``cooldown_s`` 冷却窗口。
    * 冷却到期后调用 :meth:`recover`，额度翻倍逐步回升到 ``global_limit``，
      避免刚恢复就以全速冲击对方站点。

    为什么不用 ``asyncio.Semaphore``：信号量创建后容量不可变，
    无法表达"临时减半"；而 ``Condition`` + 普通计数器可以随时改判据。
    """

    def __init__(
        self,
        global_limit: int = 8,
        per_host_limit: int = 2,
        cooldown_s: float = 30.0,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if global_limit < 1:
            raise ValueError("global_limit 至少为 1")
        if per_host_limit < 1:
            raise ValueError("per_host_limit 至少为 1")
        if per_host_limit > global_limit:
            raise ValueError("per_host_limit 不能大于 global_limit")

        self._base_limit = global_limit
        self._limit = global_limit
        self._per_host_limit = per_host_limit
        self._cooldown_s = cooldown_s
        self._clock = clock if clock is not None else time.monotonic

        self._active = 0
        self._host_active: dict[str, int] = {}
        self._cooldown_until = 0.0
        self._condition = asyncio.Condition()
        self._events: list[DegradeEvent] = []

    # -- 只读状态 ---------------------------------------------------------
    @property
    def effective_limit(self) -> int:
        """当前生效的全局并发上限。"""
        return self._limit

    @property
    def base_limit(self) -> int:
        return self._base_limit

    @property
    def cooldown_until(self) -> float:
        return self._cooldown_until

    @property
    def in_cooldown(self) -> bool:
        return self._clock() < self._cooldown_until

    @property
    def degrade_events(self) -> tuple[DegradeEvent, ...]:
        return tuple(self._events)

    @property
    def active(self) -> int:
        return self._active

    # -- 降速与恢复 -------------------------------------------------------
    def on_rate_limited(self) -> tuple[int, float]:
        """登记一次 429，返回 ``(新的并发上限, 冷却秒数)``。"""
        before = self._limit
        after = max(1, before // 2)
        self._limit = after
        self._cooldown_until = self._clock() + self._cooldown_s
        self._events.append(
            DegradeEvent(
                limit_before=before,
                limit_after=after,
                cooldown_s=self._cooldown_s,
                at=self._clock(),
            )
        )
        logger.warning(
            "触发限流降速 before=%s after=%s cooldown=%.0fs",
            before,
            after,
            self._cooldown_s,
        )
        return after, self._cooldown_s

    def recover(self) -> int:
        """若冷却已结束则把额度翻倍（上限为初始值），返回当前额度。"""
        if self._limit >= self._base_limit:
            return self._limit
        now = self._clock()
        if now < self._cooldown_until:
            return self._limit
        self._limit = min(self._base_limit, self._limit * 2)
        # 继续观察一个冷却窗口，确认对方不排斥再继续提速
        self._cooldown_until = now + self._cooldown_s
        logger.info("限流冷却结束，并发额度回升至 %s", self._limit)
        return self._limit

    # -- 闸门 -------------------------------------------------------------
    async def acquire(self, host: str = "") -> None:
        """占一个槽位；必要时等待冷却结束或额度释放。"""
        async with self._condition:
            while True:
                self.recover()
                if self.in_cooldown:
                    # 冷却期内不占用任何槽位，只等时钟推进
                    remaining = max(0.0, self._cooldown_until - self._clock())
                    try:
                        await asyncio.wait_for(
                            self._condition.wait(), timeout=max(remaining, 0.05)
                        )
                    except (asyncio.TimeoutError, TimeoutError):
                        pass
                    continue

                host_slots = self._host_active.get(host, 0)
                if self._active < self._limit and host_slots < self._per_host_limit:
                    self._active += 1
                    self._host_active[host] = host_slots + 1
                    return

                await self._condition.wait()

    async def release(self, host: str = "") -> None:
        async with self._condition:
            self._active = max(0, self._active - 1)
            host_slots = self._host_active.get(host, 0) - 1
            if host_slots > 0:
                self._host_active[host] = host_slots
            else:
                self._host_active.pop(host, None)
            self._condition.notify_all()

    @asynccontextmanager
    async def gate(self, host: str = "") -> AsyncIterator[None]:
        """``async with limiter.gate(host): ...`` 形式的槽位管理。"""
        await self.acquire(host)
        try:
            yield
        finally:
            await self.release(host)

    @classmethod
    def from_config(cls, config: ConcurrencyConfig) -> "RateLimiter":
        return cls(
            global_limit=config.global_limit,
            per_host_limit=config.per_host_limit,
            cooldown_s=config.cooldown_s,
        )


@runtime_checkable
class Fetcher(Protocol):
    """抓取器协议：编排层只依赖这两个方法。"""

    async def get(self, url: str) -> FetchResult:
        """抓取单个 URL，返回结构化结果（失败也返回，而不是抛异常）。"""
        ...

    async def aclose(self) -> None:
        """释放底层资源（连接池等）。"""
        ...


class HttpFetcher:
    """基于 aiohttp 的异步抓取器，带双层限流与指数退避重试。"""

    def __init__(
        self,
        *,
        timeout_s: float = 15.0,
        retry: RetryConfig | None = None,
        limiter: RateLimiter | None = None,
        user_agent: str = "DataPipeBot/0.1",
        session: aiohttp.ClientSession | None = None,
        sleep: Callable[[float], "asyncio.Future[None] | object"] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._retry = retry if retry is not None else RetryConfig()
        self._limiter = limiter if limiter is not None else RateLimiter()
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        self._session = session
        self._owns_session = session is None
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._rng = rng if rng is not None else random.Random()
        self._delays = backoff_delays(
            self._retry.max_retries,
            self._retry.base_delay,
            self._retry.factor,
            self._retry.jitter_ratio,
            self._rng,
        )

    # -- 生命周期 ---------------------------------------------------------
    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_s)
            )
            self._owns_session = True
        return self._session

    async def aclose(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "HttpFetcher":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- 抓取 -------------------------------------------------------------
    async def get(self, url: str) -> FetchResult:
        """抓取 URL；对 429/5xx/网络错误做指数退避重试。

        任何失败都以 ``FetchResult(error=...)`` 返回，由编排层决定
        是入待复查队列还是计数，避免异常穿透打断整批任务。
        """
        delays = self._delays
        host = urlparse(url).netloc
        started = time.perf_counter()
        attempts = 0
        last_status = 0
        last_error = "未发起请求"

        for attempt in range(len(delays) + 1):
            attempts = attempt + 1
            try:
                async with self._limiter.gate(host):
                    response = await self._get_session().get(
                        url,
                        headers=self._headers,
                        timeout=aiohttp.ClientTimeout(total=self._timeout_s),
                        allow_redirects=True,
                    )
                    try:
                        last_status = response.status
                        if 200 <= response.status < 300:
                            # aiohttp 会优先用响应头 charset，缺失时回退 utf-8
                            text = await response.text(errors="replace")
                            return FetchResult(
                                url=url,
                                status=response.status,
                                text=text,
                                elapsed_ms=int((time.perf_counter() - started) * 1000),
                                attempts=attempts,
                            )

                        if response.status == 429:
                            limit, cooldown = self._limiter.on_rate_limited()
                            retry_after = response.headers.get("Retry-After")
                            logger.warning(
                                "HTTP 429 限流 url=%s 新额度=%s 冷却=%.0fs retry_after=%s",
                                url,
                                limit,
                                cooldown,
                                retry_after,
                            )
                            last_error = "HTTP 429 Too Many Requests"
                        elif response.status in RETRYABLE_STATUS or response.status >= 500:
                            last_error = f"HTTP {response.status}"
                            logger.warning(
                                "可重试的 HTTP 状态 status=%s url=%s attempt=%s",
                                response.status,
                                url,
                                attempts,
                            )
                        else:
                            # 4xx（非 429）重试无意义，直接返回
                            return FetchResult(
                                url=url,
                                status=response.status,
                                elapsed_ms=int((time.perf_counter() - started) * 1000),
                                attempts=attempts,
                                error=f"HTTP {response.status}",
                            )
                    finally:
                        response.release()
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_status = 0
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "请求异常 url=%s attempt=%s error=%s", url, attempts, last_error
                )

            if attempt < len(delays):
                delay = delays[attempt]
                logger.info("退避重试 url=%s wait=%.2fs", url, delay)
                await self._sleep(delay)

        return FetchResult(
            url=url,
            status=last_status,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            attempts=attempts,
            error=last_error,
        )

    async def get_text(self, url: str) -> str:
        """窄接口：只要正文，失败抛 :class:`FetchError`。"""
        result = await self.get(url)
        if not result.ok:
            raise FetchError(url, result.error or "empty response", result.status or None)
        return result.text


class FixtureFetcher:
    """从本地 fixture 目录读取 HTML，让演示与测试完全离线。

    URL → 文件的映射规则：取 URL path 的最后一段作为文件名，
    例如 ``https://notice.example.gov.cn/notice/detail_001.html``
    对应 ``fixtures/detail_001.html``。特殊映射可用 ``url_map`` 覆盖。
    """

    def __init__(
        self,
        fixture_dir: str | Path,
        url_map: dict[str, str] | None = None,
        *,
        strict: bool = False,
    ) -> None:
        self._dir = Path(fixture_dir)
        self._url_map = dict(url_map or {})
        self._strict = strict
        if not self._dir.is_dir():
            raise FileNotFoundError(f"fixture 目录不存在：{self._dir}")

    @property
    def directory(self) -> Path:
        return self._dir

    def resolve(self, url: str) -> Path:
        """把 URL 解析为本地文件路径。"""
        if url in self._url_map:
            candidate = Path(self._url_map[url])
            return candidate if candidate.is_absolute() else self._dir / candidate
        name = Path(urlparse(url).path).name
        return self._dir / (name or "index.html")

    async def get(self, url: str) -> FetchResult:
        path = self.resolve(url)
        if not path.is_file():
            message = f"fixture 缺失：{path.name}"
            logger.warning("%s <- %s", message, url)
            if self._strict:
                raise FetchError(url, message, 404)
            return FetchResult(url=url, status=404, error=message, from_cache=True)

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:  # 编码错误、权限问题等
            return FetchResult(url=url, status=0, error=f"读取失败: {exc}", from_cache=True)

        return FetchResult(
            url=url,
            status=200,
            text=text,
            elapsed_ms=0,
            attempts=1,
            from_cache=True,
        )

    async def get_text(self, url: str) -> str:
        result = await self.get(url)
        if not result.ok:
            raise FetchError(url, result.error or "empty fixture", result.status or None)
        return result.text

    async def aclose(self) -> None:
        """本地读取无需释放资源，保留接口一致性。"""
        return None


def build_fetcher(
    kind: str,
    *,
    http_config: HttpConfig,
    fixture_dir: str | Path | None = None,
    url_map: dict[str, str] | None = None,
) -> Fetcher:
    """按名称构造抓取器，供 CLI 的 ``--fetcher`` 使用。"""
    normalized = (kind or "").strip().lower()
    if normalized == "http":
        return HttpFetcher(
            timeout_s=http_config.timeout_s,
            retry=http_config.retry,
            limiter=RateLimiter.from_config(http_config.concurrency),
            user_agent=http_config.user_agent,
        )
    if normalized == "fixture":
        if fixture_dir is None:
            raise ValueError("fixture 模式需要提供 fixture_dir")
        return FixtureFetcher(fixture_dir, url_map)
    raise ValueError(f"未知的 fetcher 类型：{kind!r}（可选 http / fixture）")
