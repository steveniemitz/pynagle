import logging
import time
from struct import (pack, unpack)
from cStringIO import StringIO

import gevent
from gevent.event import AsyncResult, Event
from gevent.queue import Queue

from ..core import TimerQueue
from ..message import (
  Deadline,
  MethodCallMessage,
  MethodDiscardMessage,
  MethodReturnMessage,
  ScalesClientError,
  Timeout,
  TimeoutError,
)
from ..sink import (
  AsyncMessageSink,
  ClientChannelTransportSink,
  ClientChannelSinkStack,
  ClientFormatterSink,
  ReplySink,
)
from ..constants import DispatcherState
from ..varz import (
  AggregateTimer,
  AverageTimer,
  Counter,
  Gauge,
  SourceType,
  VarzBase
)
from .formatter import (
  MessageSerializer,
  Tag
)
from .protocol import (
  Headers,
  MessageType,
)

ROOT_LOG = logging.getLogger('scales.thriftmux')

class TagPool(object):
  """A class which manages a pool of tags
    """
  POOL_LOGGER = ROOT_LOG.getChild('TagPool')

  class Varz(VarzBase):
    _VARZ_SOURCE_TYPE = SourceType.ServiceAndEndpoint
    _VARZ_BASE_NAME = 'scales.thriftmux.TagPool'
    _VARZ = {
      'pool_exhausted': Counter,
      'max_tag': Gauge
    }

  def __init__(self, max_tag, service, host):
    self._set = set()
    self._next = 1
    self._max_tag = max_tag
    self._varz = self.Varz((service, host))
    self.LOG = self.POOL_LOGGER.getChild('[%s.%s]' % (service, host))

  def get(self):
    """Get a tag from the pool.

    Returns:
      A tag

    Raises:
      Exception if the next tag will be > max_tag
    """
    if not any(self._set):
      if self._next == self._max_tag - 1:
        self._varz.pool_exhausted()
        raise Exception("No tags left in pool.")
      self._next += 1
      ret_tag = self._next
      self.LOG.debug('Allocating new tag, max is now %d' % ret_tag)
      self._varz.max_tag(ret_tag)
      return ret_tag
    else:
      return self._set.pop()

  def release(self, tag):
    """Return a tag to the pool.

    Args:
      tag - The previously leased tag.
    """
    if tag in self._set:
      self.LOG.warning('Tag %d has been returned more than once!' % tag)
    self._set.add(tag)


class SocketTransportSink(ClientChannelTransportSink):
  """A transport sink for thriftmux servers."""

  SINK_LOG = ROOT_LOG.getChild('SocketTransportSink')

  class Varz(VarzBase):
    _VARZ_BASE_NAME = 'scales.thriftmux.SocketTransportSink'
    _VARZ_SOURCE_TYPE = SourceType.ServiceAndEndpoint
    _VARZ = {
      'messages_sent': Counter,
      'messages_recv': Counter,
      'active': Gauge,
      'send_queue_size': Gauge,
      'send_time': AggregateTimer,
      'recv_time': AggregateTimer,
      'send_latency': AverageTimer,
      'recv_latency': AverageTimer
    }

  def __init__(self, socket, service):
    super(SocketTransportSink, self).__init__()
    self._tag_map = {}
    self._socket = socket
    self._send_queue = Queue()
    self._ping_timeout = 5
    self._ping_msg = self._BuildHeader(1, MessageType.Tping, 0)
    self._ping_ar = None
    self._last_ping_start = 0
    self._state = DispatcherState.UNINITIALIZED
    self._open_result = None
    self.LOG = self.SINK_LOG.getChild('[%s.%s:%d]' % (
        service, self._socket.host, self._socket.port))
    socket_source = '%s:%d' % (self._socket.host, self._socket.port)
    self._tag_pool = TagPool((2 ** 24) - 1, service, socket_source)
    self._varz = self.Varz((service, socket_source))

  @property
  def isActive(self):
    return self._state != DispatcherState.STOPPED

  def _Open(self):
    """Initializes the dispatcher, opening a connection to the remote host.
    This method may only be called once.
    """
    if self._state == DispatcherState.RUNNING:
      return
    elif self._open_result:
      self._open_result.get()
      return

    self._open_result = AsyncResult()
    try:
      self.LOG.debug('Opening transport.')
      self._socket.open()
      gevent.spawn(self._RecvLoop)
      gevent.spawn(self._SendLoop)

      self._state = DispatcherState.STARTING
      ar = self._SendPingMessage()
      ar.get()
      self.LOG.debug('Open and ping successful')
      gevent.spawn(self._PingLoop)
      self._state = DispatcherState.RUNNING
      self._open_result.set()
      self._varz.active(1)
    except Exception as e:
      self.LOG.error('Exception opening socket')
      self._open_result.set_exception(e)
      raise

  def isOpen(self):
    return self._state == DispatcherState.RUNNING

  def testConnection(self):
    # No need to actually test anything here,
    # the send and recv loops will detect connection failures
    # almost instantly.
    return self.isOpen()

  def _ShutdownImpl(self): pass

  def _Shutdown(self, reason):
    if not self.isActive:
      return

    self.LOG.warning('Shutting down transport [%s].' % str(reason))
    self._varz.active(-1)
    self._state = DispatcherState.STOPPED
    self._socket.close()
    if not isinstance(reason, Exception):
      reason = Exception(str(reason))
    self.shutdown_result.set_exception(reason)
    msg = MethodReturnMessage(error=ScalesClientError(reason))

    for sink_stack in self._tag_map.values():
      sink_stack.DispatchReplyMessage(msg)

  def _SendPingMessage(self):
    """Constucts and sends a Tping message.
    """
    self.LOG.debug('Sending ping message.')
    self._ping_ar = AsyncResult()
    self._last_ping_start = time.time()
    self._send_queue.put(self._ping_msg)
    gevent.spawn(self._PingTimeoutHelper)
    return self._ping_ar

  def _PingTimeoutHelper(self):
    ar = self._ping_ar
    ar.wait(self._ping_timeout)
    if not ar.successful():
      self._Shutdown('Ping Timeout')

  def _OnPingResponse(self, msg_type, stream):
    """Handles the response to a ping.  On failure, shuts down the dispatcher.
    """
    ar, self._ping_ar = self._ping_ar, None
    if msg_type == MessageType.Rping:
      ar.set()
      ping_duration = time.time() - self._last_ping_start
      self.LOG.debug('Got ping response in %d ms' % int(ping_duration * 1000))
    else:
      self.LOG.error('Unexpected response for tag 1 (msg_type was %d)' % msg_type)
      ar.set_exception(Exception("Invalid ping response"))

  def _PingLoop(self):
    """Periodically pings the remote server.
    """
    while self.isActive:
      gevent.sleep(30)
      if self.isActive:
        self._SendPingMessage()
      else:
        break

  def _SendLoop(self):
    """Dispatch messages from the send queue to the remote server.

    Note: Messages in the queue have already been serialized into wire format.
    """
    while self.isActive:
      try:
        payload = self._send_queue.get()
        queue_len = self._send_queue.qsize()
        self._varz.send_queue_size(queue_len)
        with self._varz.send_time.Measure():
          self._socket.write(payload)
        self._varz.messages_sent()
      except Exception as e:
        self._Shutdown(e)
        break

  def _RecvLoop(self):
    """Dispatch messages from the remote server to their recipient.

    Note: Deserialization and dispatch occurs on a seperate greenlet, this only
    reads the message off the wire.
    """
    while self.isActive:
      try:
        sz, = unpack('!i', self._socket.readAll(4))
        with self._varz.recv_time.Measure():
          buf = StringIO(self._socket.readAll(sz))
        self._varz.messages_recv()
        gevent.spawn(self._ProcessReply, buf)
      except Exception as e:
        self._Shutdown(e)
        break

  def _ProcessReply(self, stream):
    try:
      msg_type, tag = ThrfitMuxMessageSerializerSink.ReadHeader(stream)
      if tag == 1 and msg_type == MessageType.Rping: #Ping
        self._OnPingResponse(msg_type, stream)
      elif tag != 0:
        reply_stack = self._ReleaseTag(tag)
        if reply_stack:
          stream.seek(0)
          reply_stack.AsyncProcessResponse(stream)
    except Exception:
      self.LOG.exception('Exception processing reply message.')

  def _ReleaseTag(self, tag):
    """Return a tag to the tag pool.

    Note: Tags are only returned when the server has ACK'd them (or NACK'd) with
    and Rdispatch message (or similar).  Client initiated timeouts do NOT return
    tags to the pool.

    Args:
      tag - The tag to return.

    Returns:
      The ClientChannelSinkStack associated with the tag's response.
    """
    reply_stack = self._tag_map.pop(tag, None)
    self._tag_pool.release(tag)
    return reply_stack

  @staticmethod
  def _EncodeTag(tag):
    return [tag >> 16 & 0xff, tag >> 8 & 0xff, tag & 0xff] # Tag

  def _BuildHeader(self, tag, msg_type, data_len):
    total_len = 1 + 3 + data_len
    return pack('!ibBBB',
                total_len,
                msg_type,
                *self._EncodeTag(tag))

  def AsyncProcessRequest(self, sink_stack, msg, stream, headers):
    if not msg.is_one_way:
      tag = self._tag_pool.get()
      msg.properties[Tag.KEY] = tag
      self._tag_map[tag] = sink_stack
    else:
      tag = 0

    data_len = stream.tell()
    header = self._BuildHeader(tag, headers[Headers.MessageType], data_len)
    payload = header + stream.getvalue()
    self._send_queue.put(payload)


class ThrfitMuxMessageSerializerSink(ClientFormatterSink):
  class Varz(VarzBase):
    _VARZ_BASE_NAME = 'scales.thriftmux.ThrfitMuxMessageSerializerSink'
    _VARZ = {
      'deserialization_failures': Counter,
      'serialization_failures': Counter
    }

  def __init__(self, varz_source):
    super(ThrfitMuxMessageSerializerSink, self).__init__()
    self._serializer = MessageSerializer()
    self._varz = self.Varz(varz_source)

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

  def AsyncProcessMessage(self, msg, reply_sink):
    buf = StringIO()
    headers = {}
    try:
      ctx = self._serializer.Marshal(msg, buf, headers)
    except Exception as ex:
      self._varz.serialization_failures()
      msg = MethodReturnMessage(error=ex)
      reply_sink.ProcessReturnMessage(msg)
      return

    if msg.is_one_way:
      one_way_reply_sink, reply_sink = reply_sink, None
    else:
      one_way_reply_sink = None

    sink_stack = ClientChannelSinkStack(reply_sink)
    sink_stack.Push(self, ctx)
    self.next_sink.AsyncProcessRequest(sink_stack, msg, buf, headers)
    # The call is one way, so ignore the response.
    if one_way_reply_sink:
      one_way_reply_sink.ProcessReturnMessage(MethodReturnMessage())

  def AsyncProcessResponse(self, sink_stack, context, stream):
    try:
      msg_type, tag = ThrfitMuxMessageSerializerSink.ReadHeader(stream)
      msg = self._serializer.Unmarshal(tag, msg_type, stream, context)
    except Exception as ex:
      self._varz.deserialization_failures()
      msg = MethodReturnMessage(error=ex)
    sink_stack.DispatchReplyMessage(msg)


class TimeoutReplySink(ReplySink):
  LOG = logging.getLogger("scales.thriftmux.TimeoutReplySink")

  def __init__(self, client_sink, msg, next_sink, timeout):
    super(TimeoutReplySink, self).__init__()
    self.next_sink = next_sink
    self._msg = msg
    self._client_sink = client_sink
    self._varz = client_sink._varz
    self._cancel_timeout = self._client_sink._timer_queue.Schedule(timeout, self._TimeoutHelper)

  def ProcessReturnMessage(self, msg):
    self._cancel_timeout()
    if self.next_sink:
      self.next_sink.ProcessReturnMessage(msg)

  def _TimeoutHelper(self):
    """Waits for ar to be signaled or [timeout] seconds to elapse.  If the
    timeout elapses, a Tdiscarded message will be queued to the server indicating
    the client is no longer expecting a reply.
    """
    if self.next_sink:
      self._varz.timeout()
      error_msg = MethodReturnMessage(error=TimeoutError())
      reply_sink, self.next_sink = self.next_sink, None
      reply_sink.ProcessReturnMessage(error_msg)

      tag = self._msg.properties.get(Tag.KEY)
      if tag:
        msg = MethodDiscardMessage(self._msg, 'Client Timeout')
        self._client_sink.AsyncProcessMessage(msg, None)


class TimeoutSink(AsyncMessageSink):
  class Varz(VarzBase):
    _VARZ_BASE_NAME = 'scales.thriftmux.TimeoutSink.timeouts'
    _VARZ = {
      'timeouts': Counter
    }

  def __init__(self, source):
    super(TimeoutSink, self).__init__()
    self._varz = self.Varz(source)
    self._timer_queue = TimerQueue()

  def AsyncProcessMessage(self, msg, reply_sink):
    """Initialize the timeout handler for this request.

    Args:
      ar - The AsyncResult for the pending response of this request.
      timeout - An optional timeout.  If None, no timeout handler is initialized.
      tag - The tag of the request.
    """
    timeout = msg.properties.get(Timeout.KEY)
    if timeout and isinstance(msg, MethodCallMessage):
      msg.properties['com.twitter.finagle.Deadline'] = Deadline(timeout)
      reply_sink = TimeoutReplySink(self, msg, reply_sink, timeout)
    return self.next_sink.AsyncProcessMessage(msg, reply_sink)

