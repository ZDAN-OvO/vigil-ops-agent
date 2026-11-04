"""函数 Hooks 工具类

提供函数式列表挂载和装饰器链式调用两种 API，为实例方法和普通函数挂载
before / after / on_error hooks。

核心概念：
- HookContext: 统一上下文对象，包含函数名、参数、实例、返回值、异常等
- hook_before / hook_after / hook_on_error: 装饰器链式 API
- attach_hooks: 函数式列表挂载 API

使用示例::

    # ── 装饰器链式调用 ──
    @hook_before(validate_input)
    @hook_after(cache_result)
    @hook_on_error(alert_error)
    async def query(question: str, session_id: str):
        ...

    # ── 函数式列表挂载 ──
    attach_hooks(
        target=query,
        before=[validate_input],
        after=[cache_result],
        on_error=[alert_error],
    )

    # ── 实例方法 ──
    class MyService:
        @hook_before(check_auth)
        async def process(self, data: dict):
            ...
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

# ── 属性名常量 ──────────────────────────────────────────────
_ATTR_ORIGINAL = "__original_func__"
_ATTR_BEFORE = "__hooks_before__"
_ATTR_AFTER = "__hooks_after__"
_ATTR_ON_ERROR = "__hooks_on_error__"

F = TypeVar("F", bound=Callable[..., Any])


# ── HookContext ─────────────────────────────────────────────


@dataclass
class HookContext:
    """Hook 统一上下文对象

    所有 hooks 共享同一个 context 实例，可通过修改属性影响后续 hooks 和目标函数。

    Attributes:
        func_name: 目标函数名
        args: 位置参数（before hook 可修改，影响后续 hooks 和目标函数）
        kwargs: 关键字参数（before hook 可修改）
        instance: 实例方法的 self/cls（普通函数为 None）
        result: 返回值（after hook 可修改；短路时作为直接返回值）
        error: 异常（on_error hook 可置 None 吞掉异常）
        short_circuit: 短路标记（before hook 设为 True 跳过目标函数）
        state: hook 间传递数据的扩展字段
    """

    func_name: str
    args: tuple
    kwargs: dict[str, Any]
    instance: Any = None
    result: Any = None
    error: BaseException | None = None
    short_circuit: bool = False
    state: dict[str, Any] = field(default_factory=dict)


# ── 内部工具函数 ────────────────────────────────────────────


async def _run_hook(ctx: HookContext, hook: Callable) -> None:
    """执行单个 hook，自动检测同步/异步"""
    if inspect.iscoroutinefunction(hook):
        await hook(ctx)
    else:
        hook(ctx)


def _has_instance_param(func: Callable) -> bool:
    """检测函数是否有 self/cls 参数（实例方法/类方法）"""
    sig = inspect.signature(func)
    params = list(sig.parameters.values())
    return bool(params and params[0].name in ("self", "cls"))


def _build_ctx(func: Callable, args: tuple, kwargs: dict, has_self: bool) -> HookContext:
    """构建 HookContext，自动拆分 instance"""
    if has_self and args:
        return HookContext(
            func_name=func.__name__,
            args=args[1:],
            kwargs=dict(kwargs),
            instance=args[0],
        )
    return HookContext(
        func_name=func.__name__,
        args=tuple(args),
        kwargs=dict(kwargs),
    )


async def _run_before_hooks(ctx: HookContext, hooks: list[Callable]) -> bool:
    """执行 before hooks，返回是否短路"""
    for hook in hooks:
        await _run_hook(ctx, hook)
        if ctx.short_circuit:
            return True
    return False


async def _run_after_hooks(ctx: HookContext, hooks: list[Callable]) -> None:
    """执行 after hooks"""
    for hook in hooks:
        await _run_hook(ctx, hook)


async def _run_error_hooks(ctx: HookContext, hooks: list[Callable]) -> None:
    """执行 on_error hooks"""
    for hook in hooks:
        await _run_hook(ctx, hook)


# ── Wrapper 生成 ────────────────────────────────────────────


def _make_wrapper(func: Callable) -> Callable:
    """根据目标函数类型生成对应的 wrapper

    支持：同步函数、异步函数、同步生成器、异步生成器、实例方法。
    同步函数保持同步 wrapper，异步函数保持异步 wrapper。
    """
    before_hooks: list[Callable] = list(getattr(func, _ATTR_BEFORE, []))
    after_hooks: list[Callable] = list(getattr(func, _ATTR_AFTER, []))
    error_hooks: list[Callable] = list(getattr(func, _ATTR_ON_ERROR, []))
    has_self = _has_instance_param(func)

    # ── 异步生成器 ──
    if inspect.isasyncgenfunction(func):

        @functools.wraps(func)
        async def agen_wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _build_ctx(func, args, kwargs, has_self)
            if await _run_before_hooks(ctx, before_hooks):
                return
            try:
                target_args = (ctx.instance, *ctx.args) if has_self else ctx.args
                async for item in func(*target_args, **ctx.kwargs):
                    ctx.result = item
                    yield item
            except BaseException as e:
                ctx.error = e
                await _run_error_hooks(ctx, error_hooks)
                if ctx.error is not None:
                    raise ctx.error from None
                return
            await _run_after_hooks(ctx, after_hooks)

        agen_wrapper.__original_func__ = func  # type: ignore[attr-defined]
        return agen_wrapper

    # ── 同步生成器 ──
    if inspect.isgeneratorfunction(func):

        @functools.wraps(func)
        def gen_wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _build_ctx(func, args, kwargs, has_self)
            for hook in before_hooks:
                if inspect.iscoroutinefunction(hook):
                    raise TypeError(f"同步生成器的 before hook 必须是同步函数: {hook.__name__}")
                hook(ctx)
                if ctx.short_circuit:
                    return
            try:
                target_args = (ctx.instance, *ctx.args) if has_self else ctx.args
                for item in func(*target_args, **ctx.kwargs):
                    ctx.result = item
                    yield item
            except BaseException as e:
                ctx.error = e
                for hook in error_hooks:
                    if inspect.iscoroutinefunction(hook):
                        raise TypeError(
                            f"同步生成器的 on_error hook 必须是同步函数: {hook.__name__}"
                        ) from None
                    hook(ctx)
                if ctx.error is not None:
                    raise ctx.error from None
                return
            for hook in after_hooks:
                if inspect.iscoroutinefunction(hook):
                    raise TypeError(f"同步生成器的 after hook 必须是同步函数: {hook.__name__}")
                hook(ctx)

        gen_wrapper.__original_func__ = func  # type: ignore[attr-defined]
        return gen_wrapper

    # ── 异步函数 ──
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            ctx = _build_ctx(func, args, kwargs, has_self)
            if await _run_before_hooks(ctx, before_hooks):
                return ctx.result
            try:
                target_args = (ctx.instance, *ctx.args) if has_self else ctx.args
                ctx.result = await func(*target_args, **ctx.kwargs)
            except BaseException as e:
                ctx.error = e
                await _run_error_hooks(ctx, error_hooks)
                if ctx.error is not None:
                    raise ctx.error from None
            await _run_after_hooks(ctx, after_hooks)
            return ctx.result

        async_wrapper.__original_func__ = func  # type: ignore[attr-defined]
        return async_wrapper

    # ── 同步函数 ──
    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        ctx = _build_ctx(func, args, kwargs, has_self)
        for hook in before_hooks:
            if inspect.iscoroutinefunction(hook):
                raise TypeError(f"同步函数的 before hook 必须是同步函数: {hook.__name__}")
            hook(ctx)
            if ctx.short_circuit:
                return ctx.result
        try:
            target_args = (ctx.instance, *ctx.args) if has_self else ctx.args
            ctx.result = func(*target_args, **ctx.kwargs)
        except BaseException as e:
            ctx.error = e
            for hook in error_hooks:
                if inspect.iscoroutinefunction(hook):
                    raise TypeError(
                        f"同步函数的 on_error hook 必须是同步函数: {hook.__name__}"
                    ) from None
                hook(ctx)
            if ctx.error is not None:
                raise ctx.error from None
        for hook in after_hooks:
            if inspect.iscoroutinefunction(hook):
                raise TypeError(f"同步函数的 after hook 必须是同步函数: {hook.__name__}")
            hook(ctx)
        return ctx.result

    sync_wrapper.__original_func__ = func  # type: ignore[attr-defined]
    return sync_wrapper


# ── 公开 API ────────────────────────────────────────────────


def hooks_before_func(*hooks: Callable[[HookContext], Any]) -> Callable[[F], F]:
    """Before hook 装饰器

    在目标函数执行前依次执行所有 before hooks。
    hook 可通过修改 ``ctx.args`` / ``ctx.kwargs`` 影响后续 hooks 和目标函数参数，
    或设置 ``ctx.short_circuit = True`` + ``ctx.result`` 跳过目标函数直接返回。

    多个 ``@hook_before`` 叠加时，按装饰器从下到上（Python 标准）顺序执行。

    Args:
        *hooks: hook 函数，签名 ``(ctx: HookContext) -> None``

    Example::

        @hook_before(validate_input, log_params)
        async def query(question: str):
            ...
    """

    def decorator(func: F) -> F:
        original = getattr(func, _ATTR_ORIGINAL, func)
        existing = list(getattr(original, _ATTR_BEFORE, []))
        existing.extend(hooks)
        setattr(original, _ATTR_BEFORE, existing)
        return _make_wrapper(original)  # type: ignore[return-value]

    return decorator


def hooks_after_func(*hooks: Callable[[HookContext], Any]) -> Callable[[F], F]:
    """After hook 装饰器

    在目标函数执行后依次执行所有 after hooks。
    hook 可通过修改 ``ctx.result`` 改变返回值。
    """

    def decorator(func: F) -> F:
        original = getattr(func, _ATTR_ORIGINAL, func)
        existing = list(getattr(original, _ATTR_AFTER, []))
        existing.extend(hooks)
        setattr(original, _ATTR_AFTER, existing)
        return _make_wrapper(original)  # type: ignore[return-value]

    return decorator


def hooks_on_func_error(*hooks: Callable[[HookContext], Any]) -> Callable[[F], F]:
    """On-error hook 装饰器

    在目标函数抛出异常时依次执行所有 on_error hooks。
    hook 可通过 ``ctx.error`` 访问异常：

    - 将 ``ctx.error`` 设为 ``None`` 可吞掉异常，函数返回 ``ctx.result``
    - 不修改 ``ctx.error`` 则异常继续传播
    """

    def decorator(func: F) -> F:
        original = getattr(func, _ATTR_ORIGINAL, func)
        existing = list(getattr(original, _ATTR_ON_ERROR, []))
        existing.extend(hooks)
        setattr(original, _ATTR_ON_ERROR, existing)
        return _make_wrapper(original)  # type: ignore[return-value]

    return decorator


def attach_func_hooks(
    func: F,
    *,
    before: list[Callable[[HookContext], Any]] | None = None,
    after: list[Callable[[HookContext], Any]] | None = None,
    on_error: list[Callable[[HookContext], Any]] | None = None,
) -> F:
    """函数式列表挂载 API

    一次性为目标函数挂载多种 hooks，等价于依次应用装饰器。

    Args:
        func: 目标函数
        before: before hooks 列表
        after: after hooks 列表
        on_error: on_error hooks 列表

    Returns:
        包装后的函数

    Example::

        attach_hooks(
            target=query,
            before=[validate_input],
            after=[cache_result],
            on_error=[alert_error],
        )
    """
    original = getattr(func, _ATTR_ORIGINAL, func)

    if before:
        existing = list(getattr(original, _ATTR_BEFORE, []))
        existing.extend(before)
        setattr(original, _ATTR_BEFORE, existing)

    if after:
        existing = list(getattr(original, _ATTR_AFTER, []))
        existing.extend(after)
        setattr(original, _ATTR_AFTER, existing)

    if on_error:
        existing = list(getattr(original, _ATTR_ON_ERROR, []))
        existing.extend(on_error)
        setattr(original, _ATTR_ON_ERROR, existing)

    return _make_wrapper(original)  # type: ignore[return-value]
