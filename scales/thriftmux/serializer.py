from struct import (pack, unpack)

from six import string_types

from ..constants import TransportHeaders
from ..message import (
  MethodCallMessage,
  MethodDiscardMessage,
  MethodReturnMessage,
  ServerError,
  Deadline
)
from ..mux.sink import Tag
from ..thrift.serializer import MessageSerializer as ThriftMessageSerializer
from .protocol import (
  Rstatus,
  MessageType
)


class MessageSerializer(object):
  """A serializer that can serialize/deserialize method calls into the ThriftMux
  wire format."""
  def __init__(self, service_cls):
    self._marshal_map = {
      MethodCallMessage: self._Marshal_Tdispatch,
      MethodDiscardMessage: self._Marshal_Tdiscarded,
    }
    self._unmarshal_map = {
      MessageType.Rdispatch: self._Unmarshal_Rdispatch,
      MessageType.Rerr: self._Unmarshal_Rerror,
      MessageType.BAD_Rerr: self._Unmarshal_Rerror,
    }
    if service_cls:
      self._thrift_serializer = ThriftMessageSerializer(service_cls)

  def _Marshal_Tdispatch(self, msg, buf, headers):
    ctx = {}
    ctx.update(msg.public_properties)
    ctx.update(headers)
    MessageSerializer._WriteContext(ctx, buf)

    headers[TransportHeaders.MessageType] = MessageType.Tdispatch
    buf.write(pack('!hh', 0, 0))  # len(dst), len(dtab), both unsupported
    self._thrift_serializer.SerializeThriftCall(msg, buf)

  @staticmethod
  def _Marshal_Tdiscarded(msg, buf, headers):
    headers[TransportHeaders.MessageType] = MessageType.Tdiscarded
    buf.write(pack('!BBB', *Tag(msg.which).Encode()))
    buf.write(msg.reason.encode('utf-8'))

  @staticmethod
  def _WriteContext(ctx, buf):
    buf.write(pack('!h', len(ctx)))
    for k, v in ctx.items():
      if not isinstance(k, string_types):
        raise NotImplementedError("Unsupported key type in context")
      k_len = len(k)
      buf.write(pack('!h%ds' % k_len, k_len, k.encode('utf-8')))
      if isinstance(v, Deadline):
        buf.write(pack('!h', 16))
        buf.write(pack('!qq', v._ts, v._timeout))
      elif isinstance(v, string_types):
        v_len = len(v)
        buf.write(pack('!h%ds' % v_len, v_len, v.encode('utf-8')))
      else:
        raise NotImplementedError("Unsupported value type in context.")

  @staticmethod
  def _ReadContext(buf):
    for _ in range(2):
      sz, = unpack('!h', buf.read(2))
      buf.read(sz)

  def _Unmarshal_Rdispatch(self, buf):
    status, nctx = unpack('!bh', buf.read(3))
    for n in range(0, nctx):
      self._ReadContext(buf)

    if status == Rstatus.OK:
      return self._thrift_serializer.DeserializeThriftCall(buf)
    elif status == Rstatus.NACK:
      return MethodReturnMessage(error=ServerError('The server returned a NACK'))
    else:
      return MethodReturnMessage(error=ServerError(buf.read().decode('utf-8')))

  @staticmethod
  def _Unmarshal_Rerror(buf):
    why = buf.read()
    return MethodReturnMessage(error=ServerError(why.decode('utf-8')))

  def Unmarshal(self, tag, msg_type, buf):
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
    return unmarshaller(buf)

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
    marshaller(msg, buf, headers)


