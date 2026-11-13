"""
Iris Chat Memory - LLM 调用管理器

提供统一的插件内部 LLM 直接调用入口（绕过 on_llm_request 钩子链）与并发治理。
"""

from typing import Optional, Dict, List, Any, TYPE_CHECKING
import asyncio
import time

from ..core import Component, get_logger
from ..core.storage import KVStorage
from ..config import get_config
from .governor import (
    GovernorSettings,
    LLMCallGovernor,
    LLMLease,
    LLMQueueTimeoutError,
)
from .policy import CallPriority, coerce_priority

if TYPE_CHECKING:
    from astrbot.api.star import Context
    from astrbot.api.provider import LLMResponse
    from astrbot.core.provider.provider import Provider

logger = get_logger("llm_manager")


class LLMManager(Component):
    """LLM 调用管理器"""

    def __init__(self, context: "Context", storage: Optional[KVStorage] = None):
        super().__init__()
        self._context = context
        self._storage = storage
        self._governor: Optional[LLMCallGovernor] = None

    @property
    def name(self) -> str:
        return "llm_manager"

    async def initialize(self) -> None:
        try:
            config = get_config()
            self._governor = LLMCallGovernor(
                GovernorSettings(
                    global_concurrency=self._config_int(
                        config, "llm_global_concurrency", 4, minimum=1
                    ),
                    provider_concurrency=self._config_int(
                        config, "llm_provider_concurrency", 2, minimum=1
                    ),
                    provider_background_concurrency=self._config_int(
                        config,
                        "llm_provider_background_concurrency",
                        1,
                        minimum=1,
                    ),
                    provider_rpm=self._config_int(
                        config, "llm_provider_rpm", 30, minimum=0
                    ),
                    provider_min_interval_ms=self._config_int(
                        config, "llm_provider_min_interval_ms", 0, minimum=0
                    ),
                    queue_limit=self._config_int(
                        config, "llm_queue_limit", 500, minimum=1
                    ),
                    interactive_reserved_slots=self._config_int(
                        config, "llm_interactive_reserved_slots", 1, minimum=0
                    ),
                    circuit_failure_threshold=self._config_int(
                        config, "llm_circuit_failure_threshold", 5, minimum=0
                    ),
                    circuit_open_seconds=self._config_int(
                        config, "llm_circuit_open_seconds", 60, minimum=0
                    ),
                    aging_seconds=self._config_int(
                        config, "llm_priority_aging_seconds", 30, minimum=1
                    ),
                )
            )
            self._is_available = True
            logger.info("LLMManager 初始化成功")
        except Exception as e:
            self._init_error = str(e)
            logger.error(f"LLMManager 初始化失败：{e}", exc_info=True)
            raise

    async def shutdown(self) -> None:
        if self._governor:
            await self._governor.shutdown()
        self._governor = None
        self._reset_state()
        logger.info("LLMManager 已关闭")

    @staticmethod
    def _config_int(config, key: str, default: int, *, minimum: int) -> int:
        value = config.get(key, default)
        if isinstance(value, bool):
            return default
        try:
            return max(minimum, int(value))
        except (TypeError, ValueError, OverflowError):
            return default

    @property
    def governor(self) -> LLMCallGovernor:
        if self._governor is None:
            raise RuntimeError("LLM Governor 未初始化")
        return self._governor

    @staticmethod
    def _extract_usage(llm_resp: "LLMResponse") -> tuple[int, int]:
        usage = getattr(llm_resp, "usage", None)
        if not usage:
            return 0, 0
        input_tokens = int(getattr(usage, "input_other", 0) or 0) + int(
            getattr(usage, "input_cached", 0) or 0
        )
        output_tokens = int(getattr(usage, "output", 0) or 0)
        return input_tokens, output_tokens

    async def generate_direct(
        self,
        prompt: str,
        module: str = "default",
        provider_id: Optional[str] = None,
        contexts: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        timeout: Optional[float] = None,
        image_urls: Optional[List[str]] = None,
        priority: CallPriority | int | str | None = None,
        queue_timeout: Optional[float] = None,
        total_timeout: Optional[float] = None,
        **kwargs,
    ) -> str:
        """直接调用 Provider 生成文本响应（绕过 on_llm_request 钩子）"""
        if not self._is_available:
            raise RuntimeError("LLMManager 未初始化")

        actual_provider_id = await self._resolve_provider(module, provider_id)
        if not actual_provider_id:
            raise RuntimeError(
                f"未配置 Provider：module={module}。"
                f"请在插件配置中设置相应的 Provider，或在 AstrBot 中配置默认 Provider。"
            )

        provider = self._get_provider_instance(actual_provider_id)
        if not provider:
            raise RuntimeError(
                f"Provider 实例不可用：{actual_provider_id}。"
                f"请检查 AstrBot 中该 Provider 是否已启用。"
            )

        provider_timeout_sec = self._resolve_call_timeout(timeout)
        queue_timeout_sec = self._resolve_queue_timeout(queue_timeout)
        total_timeout_sec = self._resolve_total_timeout(total_timeout)
        resolved_priority = coerce_priority(priority, module)
        start_time = time.time()
        monotonic_start = time.monotonic()
        lease: Optional[LLMLease] = None

        try:
            if total_timeout_sec is not None:
                queue_timeout_sec = self._bounded_timeout(
                    queue_timeout_sec, total_timeout_sec
                )
            lease = await self.governor.acquire(
                provider_id=actual_provider_id,
                module=module,
                priority=resolved_priority,
                queue_timeout=queue_timeout_sec,
            )

            elapsed = time.monotonic() - monotonic_start
            remaining_total = None
            if total_timeout_sec is not None:
                remaining_total = total_timeout_sec - elapsed
                if remaining_total <= 0:
                    raise asyncio.TimeoutError("LLM 整体超时（排队阶段已耗尽）")
            effective_provider_timeout = self._bounded_timeout(
                provider_timeout_sec, remaining_total
            )
            llm_resp: "LLMResponse" = await self._call_with_timeout(
                provider.text_chat(
                    prompt=prompt,
                    contexts=contexts or [],
                    system_prompt=system_prompt,
                    image_urls=image_urls,
                ),
                effective_provider_timeout,
            )
            await self.governor.record_success(actual_provider_id)

            response_text = llm_resp.completion_text or ""
            duration_ms = int((time.time() - start_time) * 1000)
            input_tokens, output_tokens = self._extract_usage(llm_resp)
            logger.info(
                f"LLM 直接调用成功：module={module}, "
                f"tokens={input_tokens}+{output_tokens}, duration={duration_ms}ms"
            )
            return response_text

        except LLMQueueTimeoutError as e:
            logger.warning(f"LLM 直接调用排队超时：module={module}, error={e}")
            raise
        except asyncio.TimeoutError:
            duration_ms = int((time.time() - start_time) * 1000)
            if lease is not None:
                await self.governor.record_failure(actual_provider_id)
            logger.warning(
                f"LLM 直接调用超时：module={module}, duration={duration_ms}ms"
            )
            raise
        except Exception as e:
            if lease is not None:
                await self.governor.record_failure(actual_provider_id)
            logger.error(f"LLM 直接调用失败：module={module}, error={e}")
            raise
        finally:
            if lease is not None:
                await lease.release()

    async def _call_with_timeout(self, coro, timeout_sec: Optional[float]):
        if timeout_sec is not None:
            return await asyncio.wait_for(coro, timeout=timeout_sec)
        return await coro

    def _resolve_call_timeout(self, timeout: Optional[float]) -> Optional[float]:
        if timeout is not None:
            return timeout if timeout > 0 else None
        config = get_config()
        timeout_ms = self._configured_timeout_ms(
            config,
            "llm_provider_timeout_ms",
            legacy_key="llm_call_timeout_ms",
            default=60000,
        )
        return self._milliseconds_to_seconds(timeout_ms)

    def _resolve_queue_timeout(self, timeout: Optional[float]) -> Optional[float]:
        if timeout is not None:
            return timeout if timeout > 0 else None
        timeout_ms = self._configured_timeout_ms(
            get_config(), "llm_queue_timeout_ms", default=300000
        )
        return self._milliseconds_to_seconds(timeout_ms)

    def _resolve_total_timeout(self, timeout: Optional[float]) -> Optional[float]:
        if timeout is not None:
            return timeout if timeout > 0 else None
        timeout_ms = self._configured_timeout_ms(
            get_config(), "llm_total_timeout_ms", default=360000
        )
        return self._milliseconds_to_seconds(timeout_ms)

    @staticmethod
    def _configured_timeout_ms(
        config,
        key: str,
        *,
        default: int,
        legacy_key: Optional[str] = None,
    ) -> int:
        hidden = {}
        try:
            hidden = config.get_all_hidden()
            if not isinstance(hidden, dict):
                hidden = {}
        except Exception:
            hidden = {}
        if key in hidden:
            value = hidden[key]
        elif legacy_key and legacy_key in hidden:
            value = hidden[legacy_key]
        else:
            value = config.get(key, None)
            if value is None and legacy_key:
                value = config.get(legacy_key, default)
            if value is None:
                value = default
        if isinstance(value, bool):
            return default
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _milliseconds_to_seconds(timeout_ms: int) -> Optional[float]:
        if timeout_ms <= 0:
            return None
        return timeout_ms / 1000.0

    @staticmethod
    def _bounded_timeout(
        primary: Optional[float], bound: Optional[float]
    ) -> Optional[float]:
        if primary is None:
            return bound
        if bound is None:
            return primary
        return max(0.001, min(primary, bound))

    def _get_provider_instance(self, provider_id: str) -> Optional["Provider"]:
        try:
            if hasattr(self._context, "get_provider_by_id"):
                provider = self._context.get_provider_by_id(provider_id)
                if provider:
                    return provider

            if hasattr(self._context, "provider_manager"):
                provider_manager = self._context.provider_manager
                if hasattr(provider_manager, "inst_map"):
                    return provider_manager.inst_map.get(provider_id)
        except Exception as e:
            logger.debug(f"获取 Provider 实例失败: {e}")

        return None

    async def resolve_provider(
        self, module: str = "default", provider_id: Optional[str] = None
    ) -> Optional[str]:
        return await self._resolve_provider(module, provider_id)

    async def _resolve_provider(
        self, module: str, provider_id: Optional[str]
    ) -> Optional[str]:
        if provider_id:
            return provider_id

        config = get_config()
        module_config_map = {
            "l1_summarizer": "l1_buffer.summary_provider",
            "l3_kg_extraction": "l3_kg.extraction_provider",
            "scheduled_tasks": "scheduled_tasks.provider",
            "profile_analysis": "profile.analysis_provider",
            "l2_query_rewrite": "l2_query_rewrite_provider",
        }

        config_key = module_config_map.get(module)
        if config_key:
            configured_provider = config.get(config_key)
            if configured_provider:
                return configured_provider

        return self._get_default_provider()

    def _get_default_provider(self) -> Optional[str]:
        try:
            if hasattr(self._context, "get_config"):
                config = self._context.get_config()
                if config:
                    provider_settings = config.get("provider_settings", {})
                    default_provider_id = provider_settings.get("default_provider_id")
                    if default_provider_id:
                        return default_provider_id

            if hasattr(self._context, "provider_manager"):
                provider_manager = self._context.provider_manager
                if hasattr(provider_manager, "get_default_provider"):
                    default_provider = provider_manager.get_default_provider()
                    if default_provider and hasattr(default_provider, "id"):
                        return default_provider.id
                if hasattr(provider_manager, "providers"):
                    providers = provider_manager.providers
                    if providers and hasattr(providers[0], "id"):
                        return providers[0].id

            if hasattr(self._context, "providers"):
                providers = self._context.providers
                if providers and hasattr(providers[0], "id"):
                    return providers[0].id
        except Exception as e:
            logger.debug(f"获取默认 Provider 失败: {e}")

        return None

    async def acquire_framework_lease(
        self,
        *,
        module: str,
        provider_id: str,
        queue_timeout: Optional[float] = None,
    ) -> LLMLease:
        if not self._is_available:
            raise RuntimeError("LLMManager 未初始化")
        queue_timeout_sec = self._resolve_queue_timeout(queue_timeout)
        provider_timeout = self._resolve_call_timeout(None)
        total_timeout = self._resolve_total_timeout(None)
        watchdog_base = provider_timeout or total_timeout or 300.0
        return await self.governor.acquire(
            provider_id=provider_id,
            module=module,
            priority=CallPriority.INTERACTIVE,
            queue_timeout=queue_timeout_sec,
            lease_timeout=watchdog_base + 10.0,
        )

    async def release_framework_lease(
        self,
        lease_id: str,
        *,
        provider_id: str,
        success: bool,
    ) -> bool:
        if not lease_id or self._governor is None:
            return False
        if success:
            await self._governor.record_success(provider_id)
        else:
            await self._governor.record_failure(provider_id)
        return await self._governor.release(lease_id)

    def get_governor_metrics(self) -> Dict[str, Any]:
        if self._governor is None:
            return {
                "in_flight": 0,
                "queue_depth": 0,
                "queue_depth_by_priority": {},
            }
        return self._governor.get_metrics()

    def record_singleflight_join(self) -> None:
        if self._governor is not None:
            self._governor.record_singleflight_join()

    def record_retry(self) -> None:
        if self._governor is not None:
            self._governor.record_retry()
