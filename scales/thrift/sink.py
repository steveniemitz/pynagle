from struct import (pack, unpack)
from cStringIO import StringIO
import time

import gevent
from gevent.event import AsyncResult
from thrift.transport import TTransport

from .. import async_util
from ..constants import (ChannelState, SinkProperties)
from ..message import (
  ClientError,
  Deadline,
  MethodCallMessage,
  MethodReturnMessage,
  TimeoutError
)
from ..sink import (
  ChannelSinkProvider,
  ChannelSinkProviderBase,
  ClientMessageSink,
)
from ..thrift.socket import TSocket
from ..varz import (
  Counter,
  AggregateTimer,
  AverageTimer,
  SourceType,
  VarzBase,
  VarzSocketWrapper
)
from .formatter import MessageSerializer

class NoopTimeout(object):
  def start(self): pass
  def cancel(self): pass

class SocketTransportSink(ClientMessageSink):
  class Varz(VarzBase):
    _VARZ_BASE_NAME = 'scales.thrift.SocketTransportSink'
    _VARZ_SOURCE_TYPE = SourceType.ServiceAndEndpoint
    _VARZ = {
      'messages_sent': Counter,
      'messages_recv': Counter,
      'send_time': AggregateTimer,
      'recv_time': AggregateTimer,
      'send_latency': AverageTimer,
      'recv_latency': AverageTimer,
      'transport_latency': AverageTimer
    }

  def __init__(self, socket, source):
    super(SocketTransportSink, self).__init__()
    self._socket = socket
    self._state = ChannelState.Idle
    socket_source = '%s:%d' % (self._socket.host, self._socket.port)
    self._varz = self.Varz((source, socket_source))
    self._processing = None
    self._open_result = None

  def Open(self, force=False):
    if not self._open_result:
      self._open_result = AsyncResult()
      async_util.SafeLink(self._open_result, self._OpenImpl)
    return self._open_result

  def _OpenImpl(self):
    try:
      self._socket.open()
      self._state = ChannelState.Open
    except TTransport.TTransportException:
      self._Fault('Open failed')
      raise

  def Close(self):
    self._state = ChannelState.Closed
    self._socket.close()
    self._open_result = None
    if self._processing:
      p, self._processing = self._processing, None
      p.kill(block=False)

  @property
  def state(self):
    if self._socket.isOpen():
      return ChannelState.Open
    else:
      return self._state

  def _Fault(self, reason):
    """Shutdown the sink and signal.

    Args:
      reason - The reason the shutdown occurred.  May be an exception or string.
    """
    if self.state == ChannelState.Closed:
      return

    self.Close()
    if not isinstance(reason, Exception):
      reason = Exception(str(reason))
    self._on_faulted.Set(reason)

  def _AsyncProcessTransaction(self, data, sink_stack, deadline):
    with self._varz.transport_latency.Measure():
      gtimeout = None
      try:
        if deadline:
          timeout = deadline - time.time()
          if timeout < 0:
            raise gevent.Timeout()
          gtimeout = gevent.Timeout.start_new(timeout)
        else:
          gtimeout = NoopTimeout()

        with self._varz.send_time.Measure():
          with self._varz.send_latency.Measure():
            self._socket.write(data)
        self._varz.messages_sent()

        sz, = unpack('!i', self._socket.readAll(4))
        with self._varz.recv_time.Measure():
          with self._varz.recv_latency.Measure():
            buf = StringIO(self._socket.readAll(sz))
        self._varz.messages_recv()

        gtimeout.cancel()
        self._processing = None
        gevent.spawn(self._ProcessReply, buf, sink_stack)
      except gevent.Timeout: # pylint: disable=E0712
        err = TimeoutError()
        self._socket.close()
        self._socket.open()
        self._processing = None
        sink_stack.AsyncProcessResponseMessage(MethodReturnMessage(error=err))
      except Exception as ex:
        if gtimeout:
          gtimeout.cancel()
        self._Fault(ex)
        self._processing = None
        sink_stack.AsyncProcessResponseMessage(MethodReturnMessage(error=ex))

  @staticmethod
  def _ProcessReply(buf, sink_stack):
    try:
      buf.seek(0)
      sink_stack.AsyncProcessResponseStream(buf)
    except Exception as ex:
      sink_stack.AsyncProcessResponseMessage(MethodReturnMessage(error=ex))

  def AsyncProcessRequest(self, sink_stack, msg, stream, headers):
    if self._processing is not None:
      sink_stack.AsyncProcessResponseMessage(MethodReturnMessage(
        error=Exception('Concurrency violation in AsyncProcessRequest')))
      return

    payload = stream.getvalue()
    sz = pack('!i', len(payload))
    deadline = msg.properties.get(Deadline.KEY)
    self._processing = gevent.spawn(self._AsyncProcessTransaction, sz + payload, sink_stack, deadline)

  def AsyncProcessResponse(self, sink_stack, context, stream, msg):
    pass


class ThriftFormatterSink(ClientMessageSink):
  def __init__(self, next_provider, properties):
    super(ThriftFormatterSink, self).__init__()
    self.next_sink = next_provider.CreateSink(properties)

  def AsyncProcessRequest(self, sink_stack, msg, stream, headers):
    buf = StringIO()
    headers = {}

    if not isinstance(msg, MethodCallMessage):
      sink_stack.AsyncProcessResponseMessage(MethodReturnMessage(
          error=ClientError('Invalid message type.')))
      return
    try:
      MessageSerializer.SerializeThriftCall(msg, buf)
      ctx = msg.service, msg.method
    except Exception as ex:
      sink_stack.AsyncProcessResponseMessage(MethodReturnMessage(error=ex))
      return

    sink_stack.Push(self, ctx)
    self.next_sink.AsyncProcessRequest(sink_stack, msg, buf, headers)

  def AsyncProcessResponse(self, sink_stack, context, stream, msg):
    if msg:
      # No need to deserialize, it already is
      sink_stack.AsyncProcessResponseMessage(msg)
    else:
      try:
        msg = MessageSerializer.DeserializeThriftCall(stream, context)
      except Exception as ex:
        msg = MethodReturnMessage(error=ex)
      sink_stack.AsyncProcessResponseMessage(msg)


ThriftFormatterSinkProvider = ChannelSinkProvider(ThriftFormatterSink)

class SocketTransportSinkProvider(ChannelSinkProviderBase):
  def CreateSink(self, properties):
    server = properties[SinkProperties.Endpoint]
    service = properties[SinkProperties.Service]
    sock = TSocket.TSocket(server.host, server.port)
    healthy_sock = VarzSocketWrapper(sock, service)
    sink = SocketTransportSink(healthy_sock, service)
    return sink

  @property
  def sink_class(self):
    return SocketTransportSink
