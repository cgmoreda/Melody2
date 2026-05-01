from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from cogs.orders import ORDER_WINDOW, OrdersCog, normalize_order_item, order_acceptance_probability


class _FakeGuild:
    id = 123


class _FakeAuthor:
    def __init__(self, author_id: int) -> None:
        self.id = author_id


class _FakeContext:
    def __init__(self, author_id: int = 10) -> None:
        self.guild = _FakeGuild()
        self.author = _FakeAuthor(author_id)
        self.sent: list[tuple[Optional[str], Any]] = []

    async def send(self, content: Optional[str] = None, *, file: Any = None) -> None:
        self.sent.append((content, file))


class _FakeRepo:
    def __init__(self, totals: dict[int, float] | None = None) -> None:
        self._totals = totals or {}
        self.calls: list[dict[str, Any]] = []

    async def get_tracked_voice_totals(self, guild_id: int, *, now: Any, since: Any) -> dict[int, float]:
        self.calls.append({"guild_id": guild_id, "now": now, "since": since})
        return dict(self._totals)


def _write_asset(root: Path, item: str) -> None:
    item_dir = root / item
    item_dir.mkdir(parents=True)
    (item_dir / f"{item}-1.webp").write_bytes(b"fake webp bytes")


def test_normalize_order_item() -> None:
    assert normalize_order_item("tea") == "tea"
    assert normalize_order_item("Juice") == "juice"
    assert normalize_order_item("coffee") == "coffee"
    assert normalize_order_item("coffe") == "coffee"
    assert normalize_order_item("soda") is None
    assert normalize_order_item(None) is None


def test_order_acceptance_probability_boundaries() -> None:
    assert order_acceptance_probability("coffee", 0.0) == 1.0
    assert order_acceptance_probability("tea", 3.0) == 1.0
    assert order_acceptance_probability("juice", 10.0) == 1.0
    assert order_acceptance_probability("tea", 0.0) == pytest.approx(0.40)
    assert order_acceptance_probability("tea", 1.0) == pytest.approx(0.60)
    assert order_acceptance_probability("tea", 2.0) == pytest.approx(0.80)


@pytest.mark.asyncio
async def test_order_accepts_and_sends_random_asset(tmp_path: Path) -> None:
    _write_asset(tmp_path, "tea")
    ctx = _FakeContext(author_id=10)
    repo = _FakeRepo({10: 0.0})
    cog = OrdersCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        assets_root=tmp_path,
        rng=lambda: 0.0,
    )

    await cog.order.callback(cog, ctx, "tea")  # type: ignore[union-attr]

    assert len(ctx.sent) == 1
    content, file = ctx.sent[0]
    assert content is not None
    assert "accepts your tea order" in content
    assert file is not None
    assert repo.calls[0]["now"] - repo.calls[0]["since"] == ORDER_WINDOW


@pytest.mark.asyncio
async def test_order_refuses_non_coffee_below_probability_without_file(tmp_path: Path) -> None:
    _write_asset(tmp_path, "juice")
    ctx = _FakeContext(author_id=10)
    cog = OrdersCog(
        bot=object(),  # type: ignore[arg-type]
        repo=_FakeRepo({10: 0.0}),  # type: ignore[arg-type]
        assets_root=tmp_path,
        rng=lambda: 0.99,
    )

    await cog.order.callback(cog, ctx, "juice")  # type: ignore[union-attr]

    assert len(ctx.sent) == 1
    content, file = ctx.sent[0]
    assert content is not None
    assert "Melody says no juice" in content
    assert file is None


@pytest.mark.asyncio
async def test_order_coffee_always_accepts_at_zero_hours(tmp_path: Path) -> None:
    _write_asset(tmp_path, "coffee")
    ctx = _FakeContext(author_id=10)
    cog = OrdersCog(
        bot=object(),  # type: ignore[arg-type]
        repo=_FakeRepo({10: 0.0}),  # type: ignore[arg-type]
        assets_root=tmp_path,
        rng=lambda: 0.99,
    )

    await cog.order.callback(cog, ctx, "coffe")  # type: ignore[union-attr]

    content, file = ctx.sent[0]
    assert content is not None
    assert "accepts your coffee order" in content
    assert file is not None


@pytest.mark.asyncio
async def test_order_three_hours_always_accepts_non_coffee(tmp_path: Path) -> None:
    _write_asset(tmp_path, "tea")
    ctx = _FakeContext(author_id=10)
    cog = OrdersCog(
        bot=object(),  # type: ignore[arg-type]
        repo=_FakeRepo({10: 3 * 3600.0}),  # type: ignore[arg-type]
        assets_root=tmp_path,
        rng=lambda: 0.99,
    )

    await cog.order.callback(cog, ctx, "tea")  # type: ignore[union-attr]

    content, file = ctx.sent[0]
    assert content is not None
    assert "accepts your tea order" in content
    assert file is not None


@pytest.mark.asyncio
async def test_order_missing_assets_degrades_to_text_acceptance(tmp_path: Path) -> None:
    ctx = _FakeContext(author_id=10)
    cog = OrdersCog(
        bot=object(),  # type: ignore[arg-type]
        repo=_FakeRepo({10: 0.0}),  # type: ignore[arg-type]
        assets_root=tmp_path,
        rng=lambda: 0.0,
    )

    await cog.order.callback(cog, ctx, "tea")  # type: ignore[union-attr]

    content, file = ctx.sent[0]
    assert content is not None
    assert "photo tray is empty" in content
    assert file is None


@pytest.mark.asyncio
async def test_order_invalid_item_sends_usage_without_querying_hours(tmp_path: Path) -> None:
    repo = _FakeRepo()
    ctx = _FakeContext(author_id=10)
    cog = OrdersCog(
        bot=object(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        assets_root=tmp_path,
    )

    await cog.order.callback(cog, ctx, "soda")  # type: ignore[union-attr]

    assert ctx.sent == [("Usage: `!order <tea|juice|coffee>`", None)]
    assert repo.calls == []
