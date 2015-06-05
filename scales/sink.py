"""Sinks are classes that control and modify the flow of messages through the
 RPC system.

 Sinks cooperatively chain together in a linked list to form a _sink stack_.
 Each sink in the chain calls the next sink until the chain terminates.

 Scales has three types of sinks, both derived from MessageSink.
  - AsyncMessageSink:
      AsyncMessageSink handles dispatching a message down the sink chain.
      The chain is initiated by calling AsyncProcessMessage on the head.

  - ReplySink:
      ReplySink handles the message return path.  AsyncMessageSinks may optionally
      add their own ReplySink to the reply sink stack.

  - ClientChannelSink:
      ClientChannelSinks operate on a serialized data stream, representing the
      message.  Much like AsyncMessageSinks, they perform work, then delegate to
      the next sink until the chain terminates.

  Within ClientChannelSinks, there are also two specialized types:
    - ClientFormatterSink:
        A ClientFormatterSink acts as both a ClientChannelSink and AsyncMessageSink.
        It bridges the two, terminating the AsyncMessageSink chain and initiating the
        ClientChannelSink chain.  To do this, it serializes the message to a
        stream (in an implementation specific wire format), and calls its next
        sink as a ClientChannelSink.

    - ClientChannelTransportSink:
        ClientChannelTransportSinks act as the terminating sink of a sink chain.
        They take a serialized stream and handle transporting it to the downstream
        server, as well as handling the response.  Because of the fully asynchronous
        nature of the sink stack, the transport sink also is responsible for
        correlating requests to responses.
"""
from abc import (
  ABCMeta,
  abstractmethod,
  abstractproperty
)
from collections import deque

from gevent.event import Event

from .observable import Observable
from .message import (
  Deadline,
  MethodCallMessage,
  MethodReturnMessage,
  TimeoutError
)
from .timer_queue import GLOBAL_TIMER_QUEUE
from .varz import (
  Counter,
  VarzBase
)

class MessageSink(object):
  """A base class for all message sinks.

  MessageSinks form a cooperative linked list, which each sink calling the
  next sink in the chain once it's processing is complete.
  """
  __metaclass__ = ABCMeta
  __slots__ = '_next',

  def __init__(self):
    super(MessageSink, self).__init__()
    self._next = None

  @property
  def next_sink(self):
    """The next sink in the chain."""
    return self._next

  @next_sink.setter
  def next_sink(self, value):
    self._next = value


class ReplySink(MessageSink):
  """ReplySinks are MessageSinks for processing the asynchronous return message
  from a AsyncProcessRequest."""
  def __init__(self):
    super(ReplySink, self).__init__()

  @abstractmethod
  def ProcessReturnMessage(self, msg):
    """Performs processing on msg.

    Implementors should then call next_sink.ProcessReturnMessage(msg).

    Args:
      msg - The message to process.
    """
    raise NotImplementedError()


class AsyncMessageSink(MessageSink):
  """AsyncMessageSinks take a message, perform processing, and forward it to the
  next sink in the chain (next_sink).
  """
  def __init__(self):
    super(AsyncMessageSink, self).__init__()

  @abstractmethod
  def AsyncProcessMessage(self, msg, reply_sink):
    """Perform processing on a message.

    Args:
      msg - The message to process.
      reply_sink - A ReplySink that will receive the response message.
    """
    raise NotImplementedError()


class ClientChannelSink(MessageSink):
  """ClientChannelSinks take a message, stream, and headers and perform
  processing on them.
  """
  __slots__ = '_on_faulted',

  def __init__(self):
    self._on_faulted = Observable()
    super(ClientChannelSink, self).__init__()

  @abstractproperty
  def state(self):
    pass

  @property
  def on_faulted(self):
    return self._on_faulted

  @abstractmethod
  def Open(self, force=False):
    raise NotImplementedError()

  @abstractmethod
  def Close(self):
    raise NotImplementedError()

  @abstractmethod
  def AsyncProcessRequest(self, sink_stack, msg, stream, headers):
    """Process a request message, stream, and headers.

    Args:
      sink_stack - The SinkStack representing the processing state of the message.
                   Implementors should push their sink onto this stack before
                   forwarding the message in order to participate in processing
                   the response.
      msg - The message being processed.
      stream - A serialized version of the message.
      headers - Any additional headers to be sent.
    """
    raise NotImplementedError()

  @abstractmethod
  def AsyncProcessResponse(self, sink_stack, context, stream, msg):
    """Process a response stream.

    Args:
      sink_stack - The SinkStack representing the processing state of the message.
                   Implementors should call sink_stack.AsyncProcessMessage(...)
                   to forward the message to the next ChannelSink, or
                   sink_stack.DispatchReplyMessage to begin calling reply sinks.
      context - The context that was pushed onto the stack in AsyncProcessRequest.
      stream - The stream representing the serialized response.
    """
    raise NotImplementedError()


class ClientChannelTransportSink(ClientChannelSink):
  """ClientChannelTransportSinks represent the last sink in the chain.

  They are responsible for sending the serialized message to a remote server and
  receiving the response.
  """

  def AsyncProcessResponse(self, sink_stack, context, stream, msg):
    """AsyncProcessResponse should never be called on ClientChannelTransportSinks,
    as they are the sink responsible for handling the response.
    """
    raise Exception("This should never be called.")


class ClientFormatterSink(AsyncMessageSink, ClientChannelSink):
  """ClientFormatterSinks bridge a AsyncMessageSink and ClientChannelSink.

  They are the final AsyncMessageSink in the message sink chain, and the first
  ClientChannelSink in the channel sink chain.  Therefor, they take a message,
  serialize it to a wire format, then hand it off to the channel sink chain.
  """
  def __init__(self):
    super(ClientFormatterSink, self).__init__()

  def AsyncProcessRequest(self, sink_stack, msg, stream, headers):
    """Since ClientFormatterSinks are the first sink in the client channel sink
    chain, they should never have AsyncProcessRequest called on them."""
    raise Exception('This should never be called.')


class SinkStack(object):
  """A stack of sinks."""
  __slots__ = '_stack',

  def __init__(self):
    self._stack = deque()

  def Push(self, sink, context=None):
    """Push a sink, and optional context data, onto the stack.

    Args:
      sink - The sink to push onto the stack.
      context - Optional context data associated with the current processing
                state of the sink.
    """
    if sink is None:
      raise Exception("sink must not be None")

    self._stack.append((sink, context))

  def Pop(self):
    return self._stack.pop()


class MessageSinkStackBuilder(object):
  """A factory class responsible for creating a sink chain.
  """
  __metaclass__ = ABCMeta

  @abstractmethod
  def CreateSinkStack(self, builder):
    """Create set of message sinks.

    Args:
      name - The name of the service requesting the sinks.
    Returns:
      The head of the message sink chain.
    """
    raise NotImplementedError()


class ClientChannelSinkStack(SinkStack):
  """A SinkStack of ClientChannelSinks.

  The ClientChannelSinkStack add forwards AsyncProcessResponse to the next sink
  on the stack, or DispatchReplyMessage to the reply sink.
  """
  __slots__ = '_reply_sink',

  def __init__(self, reply_sink):
    """
    Args:
      reply_sink - An optional ReplySink.
    """
    super(ClientChannelSinkStack, self).__init__()
    self._reply_sink = reply_sink

  @property
  def reply_sink(self):
    return self._reply_sink

  @reply_sink.setter
  def reply_sink(self, value):
    self._reply_sink = value

  def DispatchReplyMessage(self, msg):
    """If a reply sink was supplied, calls ProcessReturnMessage on it.

    Args:
      msg - The message to dispatch.
    """
    if self._reply_sink:
      self._reply_sink.ProcessReturnMessage(msg)

  def AsyncProcessResponse(self, stream, msg=None):
    next_sink, next_ctx = self.Pop()
    next_sink.AsyncProcessResponse(self, next_ctx, stream, msg)

  @property
  def is_one_way(self):
    return self._reply_sink is None


class FailingChannelSink(ClientChannelSink):
  """A sink that always returns a failure message."""

  def __init__(self, ex):
    self._ex = ex
    super(FailingChannelSink, self).__init__()

  def AsyncProcessRequest(self, sink_stack, msg, stream, headers):
    msg = MethodReturnMessage(error=self._ex())
    sink_stack.AsyncProcessResponse(None, msg)

  def AsyncProcessResponse(self, sink_stack, context, stream, msg):
    raise NotImplementedError("This should never be called")

  def Open(self): pass
  def Close(self): pass
  @property
  def state(self): pass


class TimeoutReplySink(ReplySink):
  def __init__(self, client_sink, evt, next_sink, deadline):
    super(TimeoutReplySink, self).__init__()
    self.next_sink = next_sink
    self._event = evt
    self._varz = client_sink._varz
    self._cancel_timeout = GLOBAL_TIMER_QUEUE.Schedule(deadline, self._TimeoutHelper)

  def ProcessReturnMessage(self, msg):
    self._cancel_timeout()
    if self.next_sink:
      self.next_sink.ProcessReturnMessage(msg)

  def _TimeoutHelper(self):
    """Waits for ar to be signaled or [timeout] seconds to elapse.  If the
    timeout elapses, a Tdiscarded message will be queued to the server indicating
    the client is no longer expecting a reply.
    """
    self._event.set()
    if self.next_sink:
      self._varz.timeouts()
      error_msg = MethodReturnMessage(error=TimeoutError())
      reply_sink, self.next_sink = self.next_sink, None
      reply_sink.ProcessReturnMessage(error_msg)


class TimeoutSink(AsyncMessageSink):
  class Varz(VarzBase):
    _VARZ_BASE_NAME = 'scales.thriftmux.TimeoutSink'
    _VARZ = {
      'timeouts': Counter
    }

  def __init__(self, source):
    super(TimeoutSink, self).__init__()
    self._varz = self.Varz(source)

  def AsyncProcessMessage(self, msg, reply_sink):
    """Initialize the timeout handler for this request.

    Args:
      ar - The AsyncResult for the pending response of this request.
      timeout - An optional timeout.  If None, no timeout handler is initialized.
      tag - The tag of the request.
    """
    deadline = msg.properties.get(Deadline.KEY)
    if deadline and isinstance(msg, MethodCallMessage):
      evt = Event()
      msg.properties[Deadline.EVENT_KEY] = evt
      reply_sink = TimeoutReplySink(self, evt, reply_sink, deadline)
    return self.next_sink.AsyncProcessMessage(msg, reply_sink)


class ChannelSinkProviderBase(object):
  __metaclass__ = ABCMeta

  def __init__(self):
    self.next_provider = None

  @abstractmethod
  def CreateSink(self, endpoint, name, properties):
    pass


def ChannelSinkProvider(sink_cls):
  class _ChannelSinkProvider(ChannelSinkProviderBase):
    __slots__ = 'next_provider',
    __metaclass__ = ABCMeta
    SINK_CLASS = sink_cls

    def CreateSink(self, endpoint, name, properties):
      return self.SINK_CLASS(self.next_provider, endpoint, name, properties)
  return _ChannelSinkProvider
