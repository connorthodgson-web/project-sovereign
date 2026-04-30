"""User-facing chat entrypoint."""

from fastapi import APIRouter

from core.models import ChatRequest, ChatResponse
from core.transport import OperatorMessage, handle_operator_message


router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def create_chat_response(request: ChatRequest) -> ChatResponse:
    """Pass the incoming message through the shared operator transport."""
    return handle_operator_message(
        OperatorMessage(
            message=request.message,
            transport=request.transport,
            channel_id=request.channel_id,
            user_id=request.user_id,
        )
    )
