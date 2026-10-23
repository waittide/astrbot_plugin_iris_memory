"""L1 队列持久化测试。"""

from datetime import datetime

import pytest

from astrbot_plugin_iris_memory.iris_memory.l1_buffer.models import (
    ContextMessage,
    SegmentedMessageQueue,
)
from astrbot_plugin_iris_memory.iris_memory.l1_buffer.store import L1QueueStore


def _message(content: str, *, source: str = "u1", tokens: int = 1) -> ContextMessage:
    return ContextMessage(
        role="user",
        content=content,
        timestamp=datetime(2026, 10, 23, 12, 0, 0),
        token_count=tokens,
        source=source,
        metadata={"user_name": "张三", "message_id": f"m-{content}"},
        persona_id="p1",
    )


def _queue() -> SegmentedMessageQueue:
    queue = SegmentedMessageQueue(
        group_id="g1", segment_1_length=2, segment_3_length=2, total_length=6
    )
    for content in ("a", "b", "c", "d", "e"):
        queue.add_message(_message(content))
    return queue


def test_queue_survives_reopen_with_segment_order(tmp_path):
    path = tmp_path / "l1.db"
    store = L1QueueStore(path)
    store.save("g1", _queue())
    store.close()

    reopened = L1QueueStore(path)
    snapshot = reopened.load_all()
    reopened.close()

    assert list(snapshot) == ["g1"]
    segments = [segment for segment, _ in snapshot["g1"]]
    contents = [msg.content for _, msg in snapshot["g1"]]
    # 顺序固定为 L1-3 → L1-2 → L1-1，便于还原时顺序 append；
    # a/b/c 已溢出到 L1-2，d/e 仍在 L1-1
    assert segments == [2, 2, 2, 1, 1]
    assert contents == ["a", "b", "c", "d", "e"]


def test_message_fields_are_preserved(tmp_path):
    path = tmp_path / "l1.db"
    store = L1QueueStore(path)
    store.save("g1", _queue())
    snapshot = store.load_all()
    store.close()

    message = snapshot["g1"][-1][1]
    assert message.role == "user"
    assert message.token_count == 1
    assert message.source == "u1"
    assert message.metadata == {"user_name": "张三", "message_id": "m-e"}
    assert message.persona_id == "p1"
    assert message.timestamp == datetime(2026, 10, 23, 12, 0, 0)


def test_save_replaces_previous_snapshot(tmp_path):
    path = tmp_path / "l1.db"
    store = L1QueueStore(path)
    store.save("g1", _queue())

    smaller = SegmentedMessageQueue(
        group_id="g1", segment_1_length=2, segment_3_length=2, total_length=6
    )
    smaller.add_message(_message("only"))
    store.save("g1", smaller)

    snapshot = store.load_all()
    store.close()
    assert [msg.content for _, msg in snapshot["g1"]] == ["only"]


def test_delete_and_clear(tmp_path):
    path = tmp_path / "l1.db"
    store = L1QueueStore(path)
    store.save("g1", _queue())
    store.save("g2", _queue())

    store.delete("g1")
    assert list(store.load_all()) == ["g2"]

    store.clear()
    assert store.load_all() == {}
    store.close()


@pytest.mark.asyncio
async def test_buffer_restores_queue_after_restart(tmp_path, monkeypatch):
    """L1Buffer 重新初始化后应还原上次未总结的队列。"""
    from astrbot_plugin_iris_memory.iris_memory.config import init_config
    from astrbot_plugin_iris_memory.iris_memory.l1_buffer.buffer import L1Buffer
    from unittest.mock import Mock

    astrbot_config = Mock()
    astrbot_config.__getitem__ = Mock(
        return_value={"enable": True, "inject_queue_length": 20}
    )
    astrbot_config.__contains__ = Mock(return_value=True)
    init_config(astrbot_config, tmp_path)

    first = L1Buffer()
    await first.initialize()
    await first.add_message("group_1", "user", "重启前的消息", "user_1")
    await first._persist_dirty()
    await first.shutdown()

    second = L1Buffer()
    await second.initialize()
    context = second.get_context("group_1")
    await second.shutdown()

    assert [msg.content for msg in context] == ["重启前的消息"]


def test_image_queue_survives_reopen(tmp_path):
    from astrbot_plugin_iris_memory.iris_memory.image import (
        ImageInfo,
        ImageParseStatus,
        ImageQueueItem,
    )

    path = tmp_path / "l1.db"
    store = L1QueueStore(path)
    item = ImageQueueItem(
        image_hash="ph:abcd",
        image_url="https://example.com/a.jpg",
        image_info=ImageInfo(url="https://example.com/a.jpg", format="jpg"),
        message_id="m1",
        group_id="g1",
        user_id="u1",
        status=ImageParseStatus.PENDING,
        attempt_count=2,
    )
    store.save_images("g1", [item])
    store.close()

    reopened = L1QueueStore(path)
    snapshot = reopened.load_images()
    reopened.close()

    assert list(snapshot) == ["g1"]
    restored = ImageQueueItem.from_dict(snapshot["g1"][0])
    assert restored.image_hash == "ph:abcd"
    assert restored.status == ImageParseStatus.PENDING
    assert restored.message_id == "m1"
    assert restored.attempt_count == 2


@pytest.mark.asyncio
async def test_buffer_restores_pending_images(tmp_path):
    """重启后待解析图片仍可被 related 模式领取，不会遗留 [IMG:] 占位。"""
    from unittest.mock import Mock

    from astrbot_plugin_iris_memory.iris_memory.config import init_config
    from astrbot_plugin_iris_memory.iris_memory.image import (
        ImageParseStatus,
        ImageQueueItem,
    )
    from astrbot_plugin_iris_memory.iris_memory.l1_buffer.buffer import L1Buffer

    astrbot_config = Mock()
    astrbot_config.__getitem__ = Mock(
        return_value={"enable": True, "inject_queue_length": 20}
    )
    astrbot_config.__contains__ = Mock(return_value=True)
    init_config(astrbot_config, tmp_path)

    first = L1Buffer()
    await first.initialize()
    await first.add_message("group_1", "user", "[IMG:abcd] 看图", "user_1")
    first.add_image(
        "group_1",
        ImageQueueItem(
            image_hash="ph:abcd",
            image_url="https://example.com/a.jpg",
            message_id="m1",
            group_id="group_1",
            status=ImageParseStatus.PENDING,
        ),
    )
    await first._persist_dirty()
    await first.shutdown()

    second = L1Buffer()
    await second.initialize()
    pending = second.get_images("group_1")
    await second.shutdown()

    assert [img.image_hash for img in pending] == ["ph:abcd"]
