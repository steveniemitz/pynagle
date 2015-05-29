from struct import (pack, unpack)

from ..message import (
  MethodCallMessage,
  MethodDiscardMessage,
  MethodReturnMessage,
  ServerError,
  Deadline
)
from ..thrift.formatter import MessageSerializer as ThriftMessageSerializer
from .protocol import (
  Headers,
  Rstatus,
  MessageType
)

class Tag(object):
  KEY = "__Tag"

  def __init__(self, tag):
    self._tag = tag

  def Encode(self):
    return [self._tag >> 16 & 0xff,
            self._tag >>  8 & 0xff,
            self._tag       & 0xff]


class MessageSerializer(object):
  """A serializer that can serialize/deserialize method calls into the ThriftMux
  wire format."""
  def __init__(self):
    self._marshal_map = {
      MethodCallMessage: self._Marshal_Tdispatch,
      MethodDiscardMessage: self._Marshal_Tdiscarded,
    }
    self._unmarshal_map = {
      MessageType.Rdispatch: self._Unmarshal_Rdispatch,
      MessageType.Rerr: self._Unmarshal_Rerror,
      MessageType.BAD_Rerr: self._Unmarshal_Rerror,
    }

  @staticmethod
  def _Marshal_Tdispatch(msg, buf, headers):
    headers[Headers.MessageType] = MessageType.Tdispatch
    MessageSerializer._WriteContext(msg.public_properties, buf)
    buf.write(pack('!hh', 0, 0)) # len(dst), len(dtab), both unsupported
    ThriftMessageSerializer.SerializeThriftCall(msg, buf)
    # It's odd, but even "oneway" thrift messages get a response
    # with finagle, so we need to allocate a tag and track them still.
    return msg.service, msg.method

  @staticmethod
  def _Marshal_Tdiscarded(msg, buf, headers):
    headers[Headers.MessageType] = MessageType.Tdiscarded
    buf.write(pack('!BBB', *Tag(msg.which.properties[Tag.KEY]).Encode()))
    buf.write(msg.reason)
    return None,

  @staticmethod
  def _WriteContext(ctx, buf):
    buf.write(pack('!h', len(ctx)))
    for k, v in ctx.iteritems():
      if not isinstance(k, basestring):
        raise NotImplementedError("Unsupported key type in context")
      k_len = len(k)
      buf.write(pack('!h%ds' % k_len, k_len, k))
      if isinstance(v, Deadline):
        buf.write(pack('!h', 16))
        buf.write(pack('!qq', v._ts, v._timeout))
      else:
        raise NotImplementedError("Unsupported value type in context.")

  @staticmethod
  def _ReadContext(buf):
    for _ in range(2):
      sz, = unpack('!h', buf.read(2))
      buf.read(sz)

  @staticmethod
  def _Unmarshal_Rdispatch(buf, ctx):
    status, nctx = unpack('!bh', buf.read(3))
    for n in range(0, nctx):
      MessageSerializer._ReadContext(buf)

    if status == Rstatus.OK:
      return ThriftMessageSerializer.DeserializeThriftCall(buf, ctx)
    elif status == Rstatus.NACK:
      return MethodReturnMessage(error=ServerError('The server returned a NACK'))
    else:
      return MethodReturnMessage(error=ServerError(buf.read()))

  @staticmethod
  def _Unmarshal_Rerror(buf, ctx):
    why = buf.read()
    return MethodReturnMessage(error=ServerError(why))

  def Unmarshal(self, tag, msg_type, buf, ctx):
    """Deserialize a message from a stream.

    Args:
      tag - The tag of the message.
      msg_type - The message type intended to be deserialized.
      buf - The stream to deserialize from.
      ctx - The context from serialization.
    Returns:
      A MethodReturnMessage.
    """
    unmarshaller = self._unmarshal_map[msg_type]
    return unmarshaller(buf, ctx)

  def Marshal(self, msg, buf, headers):
    """Serialize a message into a stream.

    Args:
      msg - The message to serialize.
      buf - The stream to serialize into.
      headers - (out) Optional headers associated with the message.
    Returns:
      A context to be supplied during deserialization.
    """
    marshaller = self._marshal_map[msg.__class__]
    return marshaller(msg, buf, headers)


