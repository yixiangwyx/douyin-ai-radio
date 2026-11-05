"""protobuf 消息加载（frames_pb2 由 `python -m grpc_tools.protoc -I src/airadio/proto --python_out=src/airadio/proto src/airadio/proto/frames.proto` 生成）。"""
from __future__ import annotations

from . import frames_pb2 as _pb  # type: ignore

PushFrame = _pb.PushFrame
Response = _pb.Response
Message = _pb.Message
ChatMessage = _pb.ChatMessage
EmojiChatMessage = _pb.EmojiChatMessage
SocialMessage = _pb.SocialMessage
MemberMessage = _pb.MemberMessage
LikeMessage = _pb.LikeMessage
GiftMessage = _pb.GiftMessage

__all__ = [
    "PushFrame", "Response", "Message", "ChatMessage", "EmojiChatMessage",
    "SocialMessage", "MemberMessage", "LikeMessage", "GiftMessage",
]
