import logging
import random
import time
from struct import (pack, unpack)

import gevent

from ..asynchronous import AsyncResult
from ..compat import BytesIO
from ..constants import SinkProperties
from ..message import (
  Deadline,
  MethodDiscardMessage,
  MethodReturnMessage
)
from ..sink import (
  ClientMessageSink,
  SinkProvider,
  SocketTransportSinkProvider
)
from ..mux.sink import MuxSocketTransportSink
from ..varz import (
  AverageRate,
  Counter,
  Source,
  VarzBase
)
from .serializer import (
  MessageSerializer,
)
from .protocol import (
  MessageType,
)

ROOT_LOG = logging.getLogger('scales.thriftmux')


class SocketTransportSink(MuxSocketTransportSink):
  def __init__(self, socket, service):
    self._ping_timeout = 5
    self._ping_msg = self._BuildHeader(1, MessageType.Tping, 0)
    self._last_ping_start = 0
    super(SocketTransportSink, self).__init__(socket, service)

  def _Init(self):
    self._ping_ar = None
    super(SocketTransportSink, self)._Init()

  @staticmethod
  def _EncodeTag(tag):
    return [tag >> 16 & 0xff, tag >> 8 & 0xff, tag & 0xff] # Tag

  def _BuildHeader(self, tag, msg_type, data_len):
    total_len = 1 + 3 + data_len
    return pack('!ibBBB',
      total_len,
      msg_type,
      *self._EncodeTag(tag))

  def _PingLoop(self):
    """Periodically pings the remote server."""
    while self.isActive:
      gevent.sleep(random.randint(30, 40))
      if self.isActive:
        self._SendPingMessage()
      else:
        break

  def _SendPingMessage(self):
    """Constructs and sends a Tping message."""
    self._log.debug('Sending ping message.')
    self._ping_ar = AsyncResult()
    self._last_ping_start = time.time()
    self._send_queue.put((self._ping_msg, self._EMPTY_DCT))
    gevent.spawn(self._PingTimeoutHelper)
    return self._ping_ar

  def _OnPingResponse(self, msg_type, stream):
    """Handles the response to a ping.  On failure, shuts down the dispatcher.
    """
    ar, self._ping_ar = self._ping_ar, None
    if msg_type == MessageType.Rping:
      ar.set()
      ping_duration = time.time() - self._last_ping_start
      self._log.debug('Got ping response in %d ms' % int(ping_duration * 1000))
    else:
      self._log.error('Unexpected response for tag 1 (msg_type was %d)' % msg_type)
      ar.set_exception(Exception("Invalid ping response"))

  def _PingTimeoutHelper(self):
    ar = self._ping_ar
    ar.wait(self._ping_timeout)
    if not ar.successful():
      ar.set_exception(Exception('Ping timed out'))
      self._Shutdown('Ping Timeout')

  def _CheckInitialConnection(self):
    ar = self._SendPingMessage()
    ar.get()
    self._log.debug('Ping successful')
    self._greenlets.append(self._SpawnNamedGreenlet('Ping Loop', self._PingLoop))

  @staticmethod
  def _CreateDiscardMessage(tag):
    """Create a Tdiscarded message for 'tag'

    Args:
      tag - The message tag to discard.
    Returns
      A (message, buffer, headers) tuple suitable for passing to AsyncProcessRequest.
    """
    discard_message = MethodDiscardMessage(tag, 'Client timeout')
    discard_message.which = tag
    buf = BytesIO()
    headers = {}
    MessageSerializer(None).Marshal(discard_message, buf, headers)
    return discard_message, buf, headers

  def _OnTimeout(self, tag):
    if tag:
      msg, buf, headers = self._CreateDiscardMessage(tag)
      self.AsyncProcessRequest(None, msg, buf, headers)

  def _ProcessReply(self, stream):
    try:
      msg_type, tag = ThriftMuxMessageSerializerSink.ReadHeader(stream)
      if tag == 1 and msg_type == MessageType.Rping: #Ping
        self._OnPingResponse(msg_type, stream)
      elif tag != 0:
        self._ProcessTaggedReply(tag, stream)
      else:
        self._log.error('Unexpected message, msg_type = %d, tag = %d' % (msg_type, tag))
    except Exception:
      self._log.exception('Exception processing reply message.')

  def _Shutdown(self, reason, fault=True):
    super(SocketTransportSink, self)._Shutdown(reason, fault)
    if self._ping_ar:
      self._ping_ar.set_exception(reason)


SocketTransportSink.Builder = SocketTransportSinkProvider(SocketTransportSink)


class ThriftMuxMessageSerializerSink(ClientMessageSink):
  """A serializer sink that serializes thrift messages to the finagle mux
  wire format"""

  class Varz(VarzBase):
    _VARZ_BASE_NAME = 'scales.thriftmux.ThriftMuxMessageSerializerSink'
    _VARZ = {
      'deserialization_failures': Counter,
      'serialization_failures': Counter,
      'message_bytes_sent': AverageRate,
      'message_bytes_recv': AverageRate
    }

  def __init__(self, next_provider, sink_properties, global_properties):
    super(ThriftMuxMessageSerializerSink, self).__init__()
    self.next_sink = next_provider.CreateSink(global_properties)
    self._serializer = MessageSerializer(global_properties[SinkProperties.ServiceInterface])
    self._varz = self.Varz(Source(
      service=global_properties[SinkProperties.Label]))

  @staticmethod
  def ReadHeader(stream):
    """Read a mux header off a message.

    Args:
      msg - a byte buffer of raw data.

    Returns:
      A tuple of (message_type, tag)
    """
    header, = unpack('!i', stream.read(4))
    msg_type = (256 - (header >> 24 & 0xff)) * -1
    tag = ((header << 8) & 0xFFFFFFFF) >> 8
    return msg_type, tag

  def AsyncProcessRequest(self, sink_stack, msg, stream, headers):
    buf = BytesIO()
    headers = {}

    deadline = msg.properties.get(Deadline.KEY)
    if deadline:
      headers['com.twitter.finagle.Deadline'] = Deadline(deadline)

    try:
      self._serializer.Marshal(msg, buf, headers)
    except Exception as ex:
      self._varz.serialization_failures()
      msg = MethodReturnMessage(error=ex)
      sink_stack.AsyncProcessResponseMessage(msg)
      return

    self._varz.message_bytes_sent(buf.tell())
    sink_stack.Push(self)
    self.next_sink.AsyncProcessRequest(sink_stack, msg, buf, headers)

  def AsyncProcessResponse(self, sink_stack, context, stream, msg):
    if msg:
      sink_stack.AsyncProcessResponseMessage(msg)
    else:
      try:
        msg_type, tag = ThriftMuxMessageSerializerSink.ReadHeader(stream)
        msg = self._serializer.Unmarshal(tag, msg_type, stream)
        self._varz.message_bytes_recv(stream.tell())
      except Exception as ex:
        self._varz.deserialization_failures()
        msg = MethodReturnMessage(error=ex)
      sink_stack.AsyncProcessResponseMessage(msg)


ThriftMuxMessageSerializerSink.Builder = SinkProvider(ThriftMuxMessageSerializerSink)


class ClientIdInterceptorSink(ClientMessageSink):
  __slots__ = '_client_id',

  CLIENT_ID_HEADER = 'com.twitter.finagle.thrift.ClientIdContext'

  def __init__(self, next_provider, sink_properties, global_properties):
    super(ClientIdInterceptorSink, self).__init__()
    self._client_id = sink_properties.client_id
    self.next_sink = next_provider.CreateSink(global_properties)

  def AsyncProcessRequest(self, sink_stack, msg, stream, headers):
    msg.properties[self.CLIENT_ID_HEADER] = self._client_id
    self.next_sink.AsyncProcessRequest(sink_stack, msg, stream, headers)

  def AsyncProcessResponse(self, sink_stack, context, stream, msg):
    raise NotImplementedError("This should never be called")


ClientIdInterceptorSink.Builder = SinkProvider(
  ClientIdInterceptorSink,
  client_id='client')
