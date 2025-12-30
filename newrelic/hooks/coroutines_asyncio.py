# Copyright 2010 New Relic, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from newrelic.common.object_wrapper import wrap_function_wrapper, wrap_out_function
from newrelic.core.trace_cache import trace_cache


def remove_from_cache_callback(task):
    cache = trace_cache()

    # If the trace bound to this task is the root span of a transaction whose
    # completion has been deferred until other tasks finish, don't remove it
    # yet. The deferred transaction exit needs that root span to remain in the
    # cache so it can be completed later.
    try:
        trace = cache.get(id(task))
        transaction = trace and trace.transaction
        if transaction is not None and getattr(transaction, "_nr_asyncio_defer_exit_requested", False):
            if not getattr(transaction, "_nr_asyncio_defer_exit_finalizing", False):
                return
    except Exception:
        pass

    cache.task_stop(task)


def wrap_create_task(task):
    cache = trace_cache()
    trace = cache.current_trace()

    # Avoid double-linking when multiple instrumentation paths apply.
    try:
        if getattr(task, "_nr_trace_cache_linked", False):
            return task
        task._nr_trace_cache_linked = True
    except Exception:
        # Some C-accelerated task types may not allow setting attributes.
        pass

    cache.task_start(task)

    # Optional: link created tasks to the current transaction so the transaction
    # can be kept alive until all such tasks complete.
    try:
        transaction = trace and trace.transaction
        if transaction is not None:
            transaction._nr_register_asyncio_task(task)
    except Exception:
        pass

    try:
        task.add_done_callback(remove_from_cache_callback)
    except Exception:
        pass
    return task


def _instrument_event_loop(loop):
    if not loop or not hasattr(loop, "create_task"):
        return

    # Prefer instance-level wrapping, but some event loops (e.g. uvloop) may
    # not allow instance attribute assignment. In that case, fall back to
    # wrapping the class method.
    try:
        if hasattr(loop.create_task, "__wrapped__"):
            return
    except Exception:
        pass

    try:
        wrap_out_function(loop, "create_task", wrap_create_task)
        return
    except Exception:
        pass

    try:
        loop_cls = type(loop)
        if hasattr(loop_cls, "create_task") and not hasattr(loop_cls.create_task, "__wrapped__"):
            wrap_out_function(loop_cls, "create_task", wrap_create_task)
    except Exception:
        pass


def _bind_set_event_loop(loop, *args, **kwargs):
    return loop


def wrap_set_event_loop(wrapped, instance, args, kwargs):
    loop = _bind_set_event_loop(*args, **kwargs)

    _instrument_event_loop(loop)

    return wrapped(*args, **kwargs)


def wrap__lazy_init(wrapped, instance, args, kwargs):
    result = wrapped(*args, **kwargs)
    # This logic can be used for uvloop, but should
    # work for any valid custom loop factory.

    # A custom loop_factory will be used to create
    # a new event loop instance.  It will then run
    # the main() coroutine on this event loop.  Once
    # this coroutine is complete, the event loop will
    # be stopped and closed.

    # The new loop that is created and set as the
    # running loop of the duration of the run() call.
    # When the coroutine starts, it runs in the context
    # that was active when run() was called.  Any tasks
    # created within this coroutine on this new event
    # loop will inherit that context.

    # Note: The loop created by loop_factory is never
    # set as the global current loop for the thread,
    # even while it is running.
    loop = instance._loop
    _instrument_event_loop(loop)

    return result


def instrument_asyncio_base_events(module):
    wrap_out_function(module, "BaseEventLoop.create_task", wrap_create_task)


def instrument_asyncio_events(module):
    if hasattr(module, "_BaseDefaultEventLoopPolicy"):  # Python >= 3.14
        wrap_function_wrapper(module, "_BaseDefaultEventLoopPolicy.set_event_loop", wrap_set_event_loop)
    elif hasattr(module, "BaseDefaultEventLoopPolicy"):  # Python <= 3.13
        wrap_function_wrapper(module, "BaseDefaultEventLoopPolicy.set_event_loop", wrap_set_event_loop)


# For Python >= 3.11
def instrument_asyncio_runners(module):
    if hasattr(module, "Runner") and hasattr(module.Runner, "_lazy_init"):
        wrap_function_wrapper(module, "Runner._lazy_init", wrap__lazy_init)
