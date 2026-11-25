"""Polite per-retailer rate limiting, backoff, circuit breaker, and robots.txt checking."""

from __future__ import annotations

import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

logger = logging.getLogger(__name__)


class RetailerCircuitOpenError(RuntimeError):
    """Raised when a retailer exceeds the maximum number of consecutive fetch failures."""


class RobotsDisallowedError(PermissionError):
    """Raised when a URL is disallowed by the retailer's robots.txt policy."""


@dataclass
class RobotsPolicy:
    """Evaluates standard and wildcard robots.txt Allow/Disallow rules."""

    raw_text: str = ""
    allow_patterns: list[re.Pattern[str]] = field(default_factory=list)
    disallow_patterns: list[re.Pattern[str]] = field(default_factory=list)
    _stdlib_parser: RobotFileParser = field(default_factory=RobotFileParser)

    @classmethod
    def from_text(cls, robots_txt: str, user_agent: str = "*") -> RobotsPolicy:
        """Parse robots.txt content for wildcard and standard directives."""
        policy = cls(raw_text=robots_txt)
        lines = [line.split("#", 1)[0].strip() for line in robots_txt.splitlines()]
        policy._stdlib_parser.parse(lines)

        active_for_ua = False
        seen_directive_in_block = False

        for line in lines:
            if not line or ":" not in line:
                continue
            key, val = [part.strip() for part in line.split(":", 1)]
            key_lower = key.lower()
            if key_lower == "user-agent":
                if seen_directive_in_block:
                    active_for_ua = False
                    seen_directive_in_block = False
                if val == "*" or val.lower() in user_agent.lower():
                    active_for_ua = True
            elif key_lower in ("allow", "disallow"):
                seen_directive_in_block = True
                if not active_for_ua or not val:
                    continue
                pattern = cls._rule_to_regex(val)
                if key_lower == "allow":
                    policy.allow_patterns.append(pattern)
                else:
                    policy.disallow_patterns.append(pattern)

        return policy

    @staticmethod
    def _rule_to_regex(rule: str) -> re.Pattern[str]:
        """Convert a robots.txt path pattern (supporting * and $) to a compiled regex."""
        anchor_end = rule.endswith("$")
        core = rule[:-1] if anchor_end else rule
        escaped = re.escape(core).replace(r"\*", ".*")
        regex_str = f"^{escaped}{'$' if anchor_end else ''}"
        return re.compile(regex_str)

    def can_fetch(self, url: str, user_agent: str = "*") -> bool:
        """Return True if the URL is permitted by robots.txt."""
        parts = urlsplit(url)
        path_and_query = parts.path or "/"
        if parts.query:
            path_and_query = f"{path_and_query}?{parts.query}"

        # Longest-match wins between matching allow and disallow rules
        best_allow = -1
        for pat in self.allow_patterns:
            m = pat.search(path_and_query)
            if m:
                best_allow = max(best_allow, len(m.group(0)))

        best_disallow = -1
        for pat in self.disallow_patterns:
            m = pat.search(path_and_query)
            if m:
                best_disallow = max(best_disallow, len(m.group(0)))

        if best_disallow > best_allow:
            return False
        if best_allow >= best_disallow and best_allow != -1:
            return True

        return self._stdlib_parser.can_fetch(user_agent, url)


class RetailerRateLimiter:
    """Tracks per-retailer request spacing, exponential backoff, and consecutive failures."""

    def __init__(
        self,
        retailer: str,
        min_delay: float = 2.0,
        max_delay: float = 5.0,
        max_consecutive_failures: int = 5,
        sleep_fn: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if min_delay < 0 or max_delay < min_delay:
            raise ValueError(f"Invalid delay bounds: min_delay={min_delay}, max_delay={max_delay}")
        self.retailer = retailer
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_consecutive_failures = max_consecutive_failures
        self._sleep = sleep_fn
        self._rng = rng or random.Random()
        self._last_request_at: float | None = None
        self.consecutive_failures: int = 0
        self.robots_policy: RobotsPolicy | None = None

    def set_robots_txt(self, robots_txt: str, user_agent: str = "*") -> None:
        """Load robots.txt rules for this retailer."""
        self.robots_policy = RobotsPolicy.from_text(robots_txt, user_agent=user_agent)

    def check_robots(self, url: str, user_agent: str = "*") -> None:
        """Raise RobotsDisallowedError if robots.txt disallows fetching `url`."""
        if self.robots_policy and not self.robots_policy.can_fetch(url, user_agent=user_agent):
            raise RobotsDisallowedError(
                f"[{self.retailer}] URL disallowed by robots.txt: {url}"
            )

    def check_circuit(self) -> None:
        """Raise RetailerCircuitOpenError if consecutive failures reached the threshold."""
        if self.consecutive_failures >= self.max_consecutive_failures:
            raise RetailerCircuitOpenError(
                f"[{self.retailer}] Stopping after {self.consecutive_failures} consecutive failures"
            )

    def wait_before_request(self) -> float:
        """Wait a random uniform(min_delay, max_delay) interval since the previous request."""
        self.check_circuit()
        now = time.monotonic()
        if self._last_request_at is None:
            self._last_request_at = now
            return 0.0

        target_delay = self._rng.uniform(self.min_delay, self.max_delay)
        elapsed = now - self._last_request_at
        remaining = max(0.0, target_delay - elapsed)
        if remaining > 0:
            self._sleep(remaining)
        self._last_request_at = time.monotonic()
        return remaining

    def record_success(self) -> None:
        """Reset the consecutive failure counter after a successful request."""
        self.consecutive_failures = 0

    def record_failure(self, status_code: int | None = None, attempt: int = 1) -> float:
        """Record a request failure, sleep for exponential backoff on 429/503, and check circuit."""
        self.consecutive_failures += 1
        backoff = 0.0
        if status_code in (429, 502, 503, 504):
            base = min(60.0, (2.0**attempt) * self.min_delay)
            jitter = self._rng.uniform(0.5, 1.5)
            backoff = base + jitter
            logger.warning(
                "[%s] HTTP %s (failure %d/%d); backing off %.2fs",
                self.retailer,
                status_code,
                self.consecutive_failures,
                self.max_consecutive_failures,
                backoff,
            )
            self._sleep(backoff)
        self.check_circuit()
        return backoff

