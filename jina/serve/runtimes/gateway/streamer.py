import asyncio
import json
import os
import threading
from typing import (
    TYPE_CHECKING,
    AsyncIterator,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from jina._docarray import DocumentArray
from jina.excepts import ExecutorError
from jina.logging.logger import JinaLogger
from jina.proto import jina_pb2
from jina.serve.networking import GrpcConnectionPool
from jina.serve.runtimes.gateway.graph.topology_graph import TopologyGraph
from jina.serve.runtimes.gateway.async_request_response_handling import AsyncRequestResponseHandler
from jina.serve.stream import RequestStreamer
from jina.types.request import Request
from jina.types.request.data import DataRequest

__all__ = ['GatewayStreamer']

if TYPE_CHECKING:  # pragma: no cover
    from grpc.aio._interceptor import ClientInterceptor
    from opentelemetry.instrumentation.grpc._client import (
        OpenTelemetryClientInterceptor,
    )
    from opentelemetry.metrics import Meter
    from prometheus_client import CollectorRegistry


class GatewayStreamer:
    """
    Wrapper object to be used in a Custom Gateway. Naming to be defined
    """

    def __init__(
        self,
        graph_representation: Dict,
        executor_addresses: Dict[str, Union[str, List[str]]],
        graph_conditions: Dict = {},
        deployments_metadata: Dict[str, Dict[str, str]] = {},
        deployments_no_reduce: List[str] = [],
        timeout_send: Optional[float] = None,
        retries: int = 0,
        compression: Optional[str] = None,
        runtime_name: str = 'custom gateway',
        prefetch: int = 0,
        logger: Optional['JinaLogger'] = None,
        metrics_registry: Optional['CollectorRegistry'] = None,
        meter: Optional['Meter'] = None,
        aio_tracing_client_interceptors: Optional[Sequence['ClientInterceptor']] = None,
        tracing_client_interceptor: Optional['OpenTelemetryClientInterceptor'] = None,
    ):
        """
        :param graph_representation: A dictionary describing the topology of the Deployments. 2 special nodes are expected, the name `start-gateway` and `end-gateway` to
            determine the nodes that receive the very first request and the ones whose response needs to be sent back to the client. All the nodes with no outgoing nodes
            will be considered to be floating, and they will be "flagged" so that the user can ignore their tasks and not await them.
        :param executor_addresses: dictionary JSON with the input addresses of each Deployment. Each Executor can have one single address or a list of addrresses for each Executor
        :param graph_conditions: Dictionary stating which filtering conditions each Executor in the graph requires to receive Documents.
        :param deployments_metadata: Dictionary with the metadata of each Deployment. Each executor deployment can have a list of key-value pairs to
            provide information associated with the request to the deployment.
        :param deployments_no_reduce: list of Executor disabling the built-in merging mechanism.
        :param timeout_send: Timeout to be considered when sending requests to Executors
        :param retries: Number of retries to try to make successfull sendings to Executors
        :param compression: The compression mechanism used when sending requests from the Head to the WorkerRuntimes. For more details, check https://grpc.github.io/grpc/python/grpc.html#compression.
        :param runtime_name: Name to be used for monitoring.
        :param prefetch: How many Requests are processed from the Client at the same time.
        :param logger: Optional logger that can be used for logging
        :param metrics_registry: optional metrics registry for prometheus used if we need to expose metrics
        :param meter: optional OpenTelemetry meter that can provide instruments for collecting metrics
        :param aio_tracing_client_interceptors: Optional list of aio grpc tracing server interceptors.
        :param tracing_client_interceptor: Optional gprc tracing server interceptor.
        """
        self.logger = logger or JinaLogger(self.__class__.__name__)
        topology_graph = TopologyGraph(
            graph_representation=graph_representation,
            graph_conditions=graph_conditions,
            deployments_metadata=deployments_metadata,
            deployments_no_reduce=deployments_no_reduce,
            timeout_send=timeout_send,
            retries=retries,
            logger=logger,
        )

        self.runtime_name = runtime_name
        self.aio_tracing_client_interceptors = aio_tracing_client_interceptors
        self.tracing_client_interceptor = tracing_client_interceptor
        self._executor_addresses = executor_addresses

        self._connection_pool = self._create_connection_pool(
            executor_addresses,
            compression,
            metrics_registry,
            meter,
            logger,
            aio_tracing_client_interceptors,
            tracing_client_interceptor,
        )
        request_handler = AsyncRequestResponseHandler(
            metrics_registry, meter, runtime_name, logger
        )

        self._streamer = RequestStreamer(
            request_handler=request_handler.handle_request(
                graph=topology_graph, connection_pool=self._connection_pool
            ),
            result_handler=request_handler.handle_result(),
            prefetch=prefetch,
            logger=logger,
        )
        self._streamer.Call = self._streamer.stream

    def _create_connection_pool(
        self,
        deployments_addresses,
        compression,
        metrics_registry,
        meter,
        logger,
        aio_tracing_client_interceptors,
        tracing_client_interceptor,
    ):
        # add the connections needed
        connection_pool = GrpcConnectionPool(
            runtime_name=self.runtime_name,
            logger=logger,
            compression=compression,
            metrics_registry=metrics_registry,
            meter=meter,
            aio_tracing_client_interceptors=aio_tracing_client_interceptors,
            tracing_client_interceptor=tracing_client_interceptor,
        )
        for deployment_name, addresses in deployments_addresses.items():
            for address in addresses:
                connection_pool.add_connection(
                    deployment=deployment_name, address=address, head=True
                )

        return connection_pool

    def rpc_stream(self, *args, **kwargs):
        """
        stream requests from client iterator and stream responses back.

        :param args: positional arguments to be passed to inner RequestStreamer
        :param kwargs: keyword arguments to be passed to inner RequestStreamer
        :return: An iterator over the responses from the Executors
        """
        return self._streamer.stream(*args, **kwargs)

    async def stream(
        self,
        docs: DocumentArray,
        request_size: int = 100,
        return_results: bool = False,
        exec_endpoint: Optional[str] = None,
        target_executor: Optional[str] = None,
        parameters: Optional[Dict] = None,
        results_in_order: bool = False,
    ) -> AsyncIterator[Tuple[Union[DocumentArray, 'Request'], 'ExecutorError']]:
        """
        stream Documents and yield Documents or Responses and unpacked Executor error if any.

        :param docs: The Documents to be sent to all the Executors
        :param request_size: The amount of Documents to be put inside a single request.
        :param return_results: If set to True, the generator will yield Responses and not `DocumentArrays`
        :param exec_endpoint: The Executor endpoint to which to send the Documents
        :param target_executor: A regex expression indicating the Executors that should receive the Request
        :param parameters: Parameters to be attached to the Requests
        :param results_in_order: return the results in the same order as the request_iterator
        :yield: tuple of Documents or Responses and unpacked error from Executors if any
        """
        async for result in self.stream_docs(
            docs=docs,
            request_size=request_size,
            return_results=True,  # force return Responses
            exec_endpoint=exec_endpoint,
            target_executor=target_executor,
            parameters=parameters,
            results_in_order=results_in_order,
        ):
            error = None
            if jina_pb2.StatusProto.ERROR == result.status.code:
                exception = result.status.exception
                error = ExecutorError(
                    name=exception.name,
                    args=exception.args,
                    stacks=exception.stacks,
                    executor=exception.executor,
                )
            if return_results:
                yield result, error
            else:
                result.document_array_cls = DocumentArray
                yield result.data.docs, error

    async def stream_docs(
        self,
        docs: DocumentArray,
        request_size: int = 100,
        return_results: bool = False,
        exec_endpoint: Optional[str] = None,
        target_executor: Optional[str] = None,
        parameters: Optional[Dict] = None,
        results_in_order: bool = False,
    ):
        """
        stream documents and stream responses back.

        :param docs: The Documents to be sent to all the Executors
        :param request_size: The amount of Documents to be put inside a single request.
        :param return_results: If set to True, the generator will yield Responses and not `DocumentArrays`
        :param exec_endpoint: The Executor endpoint to which to send the Documents
        :param target_executor: A regex expression indicating the Executors that should receive the Request
        :param parameters: Parameters to be attached to the Requests
        :param results_in_order: return the results in the same order as the request_iterator
        :yield: Yields DocumentArrays or Responses from the Executors
        """
        from jina.types.request.data import DataRequest

        def _req_generator():
            for docs_batch in docs.batch(batch_size=request_size, shuffle=False):
                req = DataRequest()
                req.data.docs = docs_batch
                if exec_endpoint:
                    req.header.exec_endpoint = exec_endpoint
                if target_executor:
                    req.header.target_executor = target_executor
                if parameters:
                    req.parameters = parameters
                yield req

        async for resp in self.rpc_stream(
            request_iterator=_req_generator(), results_in_order=results_in_order
        ):
            if return_results:
                yield resp
            else:
                yield resp.docs

    async def close(self):
        """
        Gratefully closes the object making sure all the floating requests are taken care and the connections are closed gracefully
        """
        await self._streamer.wait_floating_requests_end()
        await self._connection_pool.close()

    Call = rpc_stream

    async def process_single_data(
        self, request: DataRequest, context=None
    ) -> DataRequest:
        """Implements request and response handling of a single DataRequest
        :param request: DataRequest from Client
        :param context: grpc context
        :return: response DataRequest
        """
        return await self._streamer.process_single_data(request, context)

    @staticmethod
    def get_streamer():
        """
        Return a streamer object based on the current environment context.
        The streamer object is contructed using runtime arguments stored in the `JINA_STREAMER_ARGS` environment variable.
        If this method is used outside a Jina context (process not controlled/orchestrated by jina), this method will
        raise an error.
        The streamer object does not have tracing/instrumentation capabilities.

        :return: Returns an instance of `GatewayStreamer`
        """
        if 'JINA_STREAMER_ARGS' in os.environ:
            args_dict = json.loads(os.environ['JINA_STREAMER_ARGS'])
            return GatewayStreamer(**args_dict)
        else:
            raise OSError('JINA_STREAMER_ARGS environment variable is not set')

    @staticmethod
    def _set_env_streamer_args(**kwargs):
        os.environ['JINA_STREAMER_ARGS'] = json.dumps(kwargs)

    async def warmup(self, stop_event: threading.Event):
        """Executes warmup task on each deployment. This forces the gateway to establish connection and open a
        gRPC channel to each executor so that the first request doesn't need to experience the penalty of
        eastablishing a brand new gRPC channel.
        :param stop_event: signal to indicate if an early termination of the task is required for graceful teardown.
        """
        self.logger.debug(f'Running GatewayRuntime warmup')
        deployments = {key for key in self._executor_addresses.keys()}

        try:
            deployment_warmup_tasks = []
            try:
                for deployment in deployments:
                    deployment_warmup_tasks.append(
                        asyncio.create_task(
                            self._connection_pool.warmup(
                                deployment=deployment, stop_event=stop_event
                            )
                        )
                    )

                await asyncio.gather(*deployment_warmup_tasks, return_exceptions=True)
            except asyncio.CancelledError:
                if deployment_warmup_tasks:
                    for task in deployment_warmup_tasks:
                        task.cancel()
                raise
        except Exception as ex:
            self.logger.error(f'error with GatewayRuntime warmup up task: {ex}')
            return


class _ExecutorStreamer:
    def __init__(self, connection_pool: GrpcConnectionPool, executor_name: str) -> None:
        self._connection_pool: GrpcConnectionPool = connection_pool
        self.executor_name = executor_name

    async def post(
        self,
        inputs: DocumentArray,
        request_size: int = 100,
        on: Optional[str] = None,
        parameters: Optional[Dict] = None,
        **kwargs,
    ):
        reqs = []
        for docs_batch in inputs.batch(batch_size=request_size, shuffle=False):
            req = DataRequest()
            req.header.exec_endpoint = on
            req.header.target_executor = self.executor_name
            req.parameters = parameters
            req.data.docs = docs_batch
            reqs.append(req)

        tasks = [
            self._connection_pool.send_requests_once(
                requests=[req], deployment=self.executor_name, head=True, endpoint=on
            )
            for req in reqs
        ]

        results = await asyncio.gather(*tasks)

        docs = DocumentArray.empty()
        for resp, _ in results:
            docs.extend(resp.docs)

        return docs
