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


def remove_from_cache(task):
    cache = trace_cache()
    cache.task_stop(task)


def propagate_task_context(task):
    # Avoid double propagation if multiple layers (loop.create_task,
    # asyncio.create_task, asyncio.ensure_future) are wrapped.
    try:
        if getattr(task, "_nr_task_context_propagated", False):
            return task
        setattr(task, "_nr_task_context_propagated", True)
    except Exception:
        # Task implementations may not allow setting attributes (e.g. C-extensions).
        pass

    # Snapshot trace/span linking metadata at scheduling time so it can still be
    # retrieved from inside the task even if the originating transaction is
    # ended before the task runs (e.g. aiohttp returning the response early).
    trace = trace_cache().current_trace()
    if trace:
        try:
            linking_metadata = trace._get_trace_linking_metadata()
        except Exception:
            linking_metadata = None

        if linking_metadata:
            try:
                setattr(task, "_nr_trace_linking_metadata", linking_metadata)
            except Exception:
                # Some task implementations may not allow setting attributes.
                pass

    trace_cache().task_start(task)
    task.add_done_callback(remove_from_cache)
    return task


def _bind_loop(loop, *args, **kwargs):
    return loop


def wrap_create_task(wrapped, instance, args, kwargs):
    loop = _bind_loop(*args, **kwargs)

    if loop and not hasattr(loop.create_task, "__wrapped__"):
        wrap_out_function(loop, "create_task", propagate_task_context)

    return wrapped(*args, **kwargs)


def instrument_asyncio_base_events(module):
    wrap_out_function(module, "BaseEventLoop.create_task", propagate_task_context)


def instrument_asyncio_events(module):
    wrap_function_wrapper(module, "BaseDefaultEventLoopPolicy.set_event_loop", wrap_create_task)


def instrument_asyncio_tasks(module):
    # Wrap high-level task creation helpers. This is critical for uvloop where
    # the loop object may not be patchable, or may not go through the default
    # event loop policy implementations.
    if hasattr(module, "create_task"):
        wrap_out_function(module, "create_task", propagate_task_context)
    if hasattr(module, "ensure_future"):
        wrap_out_function(module, "ensure_future", propagate_task_context)


def instrument_asyncio(module):
    # Also patch the top-level asyncio module exports if present. These can be
    # imported by value (asyncio.create_task), so patching asyncio.tasks alone
    # may not affect already-imported references.
    if hasattr(module, "create_task"):
        wrap_out_function(module, "create_task", propagate_task_context)
    if hasattr(module, "ensure_future"):
        wrap_out_function(module, "ensure_future", propagate_task_context)


def instrument_uvloop(module):
    """Instrument uvloop task creation.

    uvloop's loop implementation is a C extension; patching a loop instance can
    fail if attributes are read-only. Patching at the class level is more
    reliable.
    """
    Loop = getattr(module, "Loop", None)
    if Loop is not None and hasattr(Loop, "create_task"):
        try:
            wrap_out_function(Loop, "create_task", propagate_task_context)
        except Exception:
            # If uvloop doesn't allow patching (implementation detail), fall back
            # to asyncio.* wrappers which cover most user code paths.
            pass
