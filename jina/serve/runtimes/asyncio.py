import argparse
import asyncio
import signal
import threading
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional, Union

from grpc import RpcError

from jina.constants import __windows__
from jina.helper import send_telemetry_event
from jina.serve.instrumentation import InstrumentationMixin
from jina.serve.networking.utils import send_health_check_async, send_health_check_sync
from jina.serve.runtimes.base import BaseRuntime
from jina.serve.runtimes.monitoring import MonitoringMixin
from jina.types.request.data import DataRequest

if TYPE_CHECKING:  # pragma: no cover
    import multiprocessing

HANDLED_SIGNALS = (
    signal.SIGINT,  # Unix signal 2. Sent by Ctrl+C.
    signal.SIGTERM,  # Unix signal 15. Sent by `kill <pid>`.
    signal.SIGSEGV,
)


class AsyncNewLoopRuntime(BaseRuntime, MonitoringMixin, InstrumentationMixin, ABC):
    """
    The async runtime to start a new event loop.
    """

    def __init__(
        self,
        args: 'argparse.Namespace',
        cancel_event: Optional[
            Union['asyncio.Event', 'multiprocessing.Event', 'threading.Event']
        ] = None,
        **kwargs,
    ):
        super().__init__(args, **kwargs)
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self.is_cancel = cancel_event or asyncio.Event()

        if not __windows__:

            def _cancel(sig):
                def _inner_cancel(*args, **kwargs):
                    self.logger.debug(f'Received signal {sig.name}')
                    self.is_cancel.set(),

                return _inner_cancel

            for sig in HANDLED_SIGNALS:
                self._loop.add_signal_handler(sig, _cancel(sig), sig, None)
        else:

            def _cancel(signum, frame):
                self.logger.debug(f'Received signal {signum}')
                self.is_cancel.set(),

            for sig in HANDLED_SIGNALS:
                signal.signal(sig, _cancel)

        self._setup_monitoring()
        self._setup_instrumentation(
            name=self.args.name,
            tracing=self.args.tracing,
            traces_exporter_host=self.args.traces_exporter_host,
            traces_exporter_port=self.args.traces_exporter_port,
            metrics=self.args.metrics,
            metrics_exporter_host=self.args.metrics_exporter_host,
            metrics_exporter_port=self.args.metrics_exporter_port,
        )
        self._start_time = time.time()
        self._loop.run_until_complete(self.async_setup())
        self._send_telemetry_event()
        self.warmup_task = None
        self.warmup_stop_event = threading.Event()

    def _send_telemetry_event(self):
        send_telemetry_event(event='start', obj=self, entity_id=self._entity_id)

    def run_forever(self):
        """
        Running method to block the main thread.

        Run the event loop until a Future is done.
        """
        self._loop.run_until_complete(self._loop_body())

    def _teardown_instrumentation(self):
        try:
            if self.tracing and self.tracer_provider:
                if hasattr(self.tracer_provider, 'force_flush'):
                    self.tracer_provider.force_flush()
                if hasattr(self.tracer_provider, 'shutdown'):
                    self.tracer_provider.shutdown()
            if self.metrics and self.meter_provider:
                if hasattr(self.meter_provider, 'force_flush'):
                    self.meter_provider.force_flush()
                if hasattr(self.meter_provider, 'shutdown'):
                    self.meter_provider.shutdown()
        except Exception as ex:
            self.logger.warning(f'Exception during instrumentation teardown, {str(ex)}')

    def teardown(self):
        """Call async_teardown() and stop and close the event loop."""
        self._teardown_instrumentation()
        self._loop.run_until_complete(self.async_teardown())
        self._loop.stop()
        self._loop.close()
        super().teardown()
        self._stop_time = time.time()
        send_telemetry_event(
            event='stop',
            obj=self,
            duration=self._stop_time - self._start_time,
            entity_id=self._entity_id,
        )

    async def _wait_for_cancel(self):
        """Do NOT override this method when inheriting from :class:`GatewayPod`"""
        # threads are not using asyncio.Event, but threading.Event
        if isinstance(self.is_cancel, asyncio.Event):
            await self.is_cancel.wait()
        else:
            while not self.is_cancel.is_set():
                await asyncio.sleep(0.1)

        await self.async_cancel()

    async def _loop_body(self):
        """Do NOT override this method when inheriting from :class:`GatewayPod`"""
        try:
            await asyncio.gather(self.async_run_forever(), self._wait_for_cancel())
        except asyncio.CancelledError:
            self.logger.warning('received terminate ctrl message from main process')

    def _cancel(self):
        """
        Signal the runtime to terminate
        """
        self.is_cancel.set()

    async def async_setup(self):
        """The async method to setup."""
        pass

    async def async_teardown(self):
        """The async method to clean up resources during teardown. This method should free all resources allocated
        during async_setup"""
        pass

    @abstractmethod
    async def async_cancel(self):
        """An async method to cancel async_run_forever."""
        ...

    @abstractmethod
    async def async_run_forever(self):
        """The async method to run until it is stopped."""
        ...

    def cancel_warmup_task(self):
        """Cancel warmup task if exists and is not completed. Cancellation is required if the Flow is being terminated before the
        task is successful or hasn't reached the max timeout.
        """
        if self.warmup_task:
            try:
                if not self.warmup_task.done():
                    self.logger.debug(f'Cancelling warmup task.')
                    self.warmup_stop_event.set()  # this event is useless if simply cancel
                    self.warmup_task.cancel()
            except Exception as ex:
                self.logger.debug(f'exception during warmup task cancellation: {ex}')
                pass

    # Static methods used by the Pod to communicate with the `Runtime` in the separate process

    @staticmethod
    def is_ready(ctrl_address: str, timeout: float = 1.0, **kwargs) -> bool:
        """
        Check if status is ready.

        :param ctrl_address: the address where the control request needs to be sent
        :param timeout: timeout of the health check in seconds
        :param kwargs: extra keyword arguments

        :return: True if status is ready else False.
        """
        try:
            from grpc_health.v1 import health_pb2, health_pb2_grpc

            response = send_health_check_sync(ctrl_address, timeout=timeout)
            return (
                response.status == health_pb2.HealthCheckResponse.ServingStatus.SERVING
            )
        except RpcError:
            return False

    @staticmethod
    async def async_is_ready(ctrl_address: str, timeout: float = 1.0, **kwargs) -> bool:
        """
        Async Check if status is ready.
        :param ctrl_address: the address where the control request needs to be sent
        :param timeout: timeout of the health check in seconds
        :param kwargs: extra keyword arguments
        :return: True if status is ready else False.
        """
        try:
            from grpc_health.v1 import health_pb2, health_pb2_grpc

            response = await send_health_check_async(ctrl_address, timeout=timeout)
            return (
                response.status == health_pb2.HealthCheckResponse.ServingStatus.SERVING
            )
        except RpcError:
            return False

    @classmethod
    def wait_for_ready_or_shutdown(
        cls,
        timeout: Optional[float],
        ready_or_shutdown_event: Union['multiprocessing.Event', 'threading.Event'],
        ctrl_address: str,
        health_check: bool = False,
        **kwargs,
    ):
        """
        Check if the runtime has successfully started

        :param timeout: The time to wait before readiness or failure is determined
        :param ctrl_address: the address where the control message needs to be sent
        :param ready_or_shutdown_event: the multiprocessing event to detect if the process failed or is ready
        :param health_check: if true, a grpc health check will be used instead of relying on the event
        :param kwargs: extra keyword arguments
        :return: True if is ready or it needs to be shutdown
        """
        timeout_ns = 1000000000 * timeout if timeout else None
        now = time.time_ns()
        if health_check:
            return cls.is_ready(ctrl_address, timeout)
        while timeout_ns is None or time.time_ns() - now < timeout_ns:
            if ready_or_shutdown_event.is_set() or cls.is_ready(ctrl_address, **kwargs):
                return True
            time.sleep(0.1)
        return False

    @property
    def _entity_id(self):
        import uuid

        if hasattr(self, '_entity_id_'):
            return self._entity_id_
        self._entity_id_ = uuid.uuid1().hex
        return self._entity_id_
