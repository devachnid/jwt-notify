"""Proxy routes mirroring the GOV.UK Notify REST API.

Paths match Notify's own, so a client built against these routes reads exactly
like one built against Notify — the only difference being that this service
supplies the JWT.

Each route declares the status codes and body schemas Notify actually returns —
201 for a send, 400 and friends for a rejection. That matters beyond the
documentation: the Azure custom connector builds a Logic App's output fields
from these schemas, so an operation described only as "200, empty schema" hands
the designer nothing to pick from.

Sending ``X-Notify-Envelope: true`` wraps the answer instead, so that a caller
which cannot read the body of a failed call still gets the status and the error
detail as fields. See :class:`app.models.NotifyEnvelope`.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

from .models import (
    EmailRequest,
    EmailResponse,
    LetterRequest,
    LetterResponse,
    NotificationListResponse,
    NotificationResponse,
    NotificationStatus,
    NotifyError,
    PrecompiledLetterRequest,
    PrecompiledLetterResponse,
    ReceivedTextMessageListResponse,
    SmsRequest,
    SmsResponse,
    TemplateListResponse,
    TemplatePreviewRequest,
    TemplatePreviewResponse,
    TemplateResponse,
    TemplateType,
)
from .auth import require_proxy_key
from .notify_client import NotifyClient, NotifyUnavailableError

router = APIRouter(tags=["GOV.UK Notify"])


def get_client(request: Request) -> NotifyClient:
    client: NotifyClient | None = getattr(request.app.state, "notify_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Proxying is not configured: set NOTIFY_API_KEY to the Notify API "
                "key this service should sign with."
            ),
        )
    return client


#: Header that asks for :class:`NotifyEnvelope` instead of a passthrough.
ENVELOPE_HEADER = "X-Notify-Envelope"

_TRUTHY = {"1", "true", "yes", "on"}


async def wants_envelope(
    # Hidden from the schema for the same reason as the proxy key: the
    # connector built with --envelope sends it as a fixed header, so it is not
    # something a Logic App author should have to fill in per operation.
    x_notify_envelope: Annotated[
        str | None, Header(alias=ENVELOPE_HEADER, include_in_schema=False)
    ] = None,
) -> bool:
    return (x_notify_envelope or "").strip().lower() in _TRUTHY


Client = Annotated[NotifyClient, Depends(get_client)]
Enveloped = Annotated[bool, Depends(wants_envelope)]
Authorised = Depends(require_proxy_key)


#: Notify's documented failures, described so that the connector — and anyone
#: reading the generated documentation — sees the same status codes as
#: https://docs.notifications.service.gov.uk/rest-api.html rather than this
#: service's defaults.
NOTIFY_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": NotifyError, "description": "Notify rejected the request"},
    403: {"model": NotifyError, "description": "Notify rejected the credentials"},
    429: {"model": NotifyError, "description": "Notify rate limit exceeded"},
    500: {"model": NotifyError, "description": "Notify had a problem"},
}


def _envelope(
    status_code: int, body: Any | None, errors: list[dict[str, Any]] | None = None
) -> JSONResponse:
    """Wrap Notify's answer in a 200 so the caller can read it either way."""
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status_code": status_code,
            "success": 200 <= status_code < 300,
            "body": body,
            "errors": errors or [],
        },
    )


def _passthrough(response: httpx.Response, *, envelope: bool = False) -> Response:
    """Return Notify's response as our own.

    Status code and body are preserved so that callers see Notify's own
    validation errors and rate-limit responses rather than a rewrite of them.

    In envelope mode a JSON response is wrapped instead: the status code moves
    into the body and the call comes back 200, so a client that treats any 4xx
    as a dead end still gets to see why Notify refused. Anything that is not
    JSON — a letter PDF — is passed through whatever was asked for, there being
    no sensible way to wrap it.
    """
    headers = {}
    if "retry-after" in response.headers:
        headers["Retry-After"] = response.headers["retry-after"]

    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            payload = response.json()
        except ValueError:
            pass
        else:
            if envelope:
                errors = payload.get("errors") if isinstance(payload, dict) else None
                return _envelope(
                    response.status_code,
                    # Every Notify response is an object; anything else is
                    # nested rather than dropped, so the envelope's own shape
                    # holds whatever arrives.
                    payload if isinstance(payload, dict) else {"body": payload},
                    errors if isinstance(errors, list) else None,
                )
            return JSONResponse(
                status_code=response.status_code, content=payload, headers=headers
            )

    return Response(
        status_code=response.status_code,
        content=response.content,
        media_type=content_type or None,
        headers=headers,
    )


async def _forward(
    client: NotifyClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any | None = None,
    envelope: bool = False,
) -> Response:
    try:
        upstream = await client.request(method, path, params=params, json=json)
    except NotifyUnavailableError as exc:
        status_code = (
            status.HTTP_504_GATEWAY_TIMEOUT if exc.timeout else status.HTTP_502_BAD_GATEWAY
        )
        if envelope:
            # Not reaching Notify is a status like any other in envelope mode:
            # the point of the mode is that the caller never has to handle a
            # failed call to read what happened.
            return _envelope(
                status_code,
                None,
                [{"error": "NotifyUnavailableError", "message": str(exc)}],
            )
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return _passthrough(upstream, envelope=envelope)


# --- sending -----------------------------------------------------------------


@router.post(
    "/v2/notifications/sms",
    summary="Send a text message",
    operation_id="SendSms",
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"model": SmsResponse, "description": "Notify accepted the text message"},
        **NOTIFY_ERRORS,
    },
    dependencies=[Authorised],
)
async def send_sms(body: SmsRequest, client: Client, envelope: Enveloped) -> Response:
    return await _forward(
        client,
        "POST",
        "/v2/notifications/sms",
        json=body.model_dump(exclude_none=True),
        envelope=envelope,
    )


@router.post(
    "/v2/notifications/email",
    summary="Send an email",
    operation_id="SendEmail",
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"model": EmailResponse, "description": "Notify accepted the email"},
        **NOTIFY_ERRORS,
    },
    dependencies=[Authorised],
)
async def send_email(body: EmailRequest, client: Client, envelope: Enveloped) -> Response:
    return await _forward(
        client,
        "POST",
        "/v2/notifications/email",
        json=body.model_dump(exclude_none=True),
        envelope=envelope,
    )


@router.post(
    "/v2/notifications/letter",
    summary="Send a letter from a template",
    operation_id="SendLetter",
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"model": LetterResponse, "description": "Notify accepted the letter"},
        **NOTIFY_ERRORS,
    },
    dependencies=[Authorised],
)
async def send_letter(body: LetterRequest, client: Client, envelope: Enveloped) -> Response:
    return await _forward(
        client,
        "POST",
        "/v2/notifications/letter",
        json=body.model_dump(exclude_none=True),
        envelope=envelope,
    )


@router.post(
    "/v2/notifications/letter/precompiled",
    summary="Send a precompiled letter (a ready-made PDF)",
    operation_id="SendPrecompiledLetter",
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {
            "model": PrecompiledLetterResponse,
            "description": "Notify accepted the letter",
        },
        **NOTIFY_ERRORS,
    },
    dependencies=[Authorised],
)
async def send_precompiled_letter(
    body: PrecompiledLetterRequest, client: Client, envelope: Enveloped
) -> Response:
    # Notify takes both letter kinds on /v2/notifications/letter, distinguished
    # by the body. They are split here so each connector operation has one
    # schema instead of a oneOf, which Swagger 2.0 cannot express.
    return await _forward(
        client,
        "POST",
        "/v2/notifications/letter",
        json=body.model_dump(exclude_none=True),
        envelope=envelope,
    )


# --- message status ----------------------------------------------------------


@router.get(
    "/v2/notifications/{notification_id}",
    summary="Get the status of one message",
    operation_id="GetNotification",
    responses={
        200: {"model": NotificationResponse, "description": "The message and its status"},
        **NOTIFY_ERRORS,
    },
    dependencies=[Authorised],
)
async def get_notification(
    notification_id: UUID, client: Client, envelope: Enveloped
) -> Response:
    return await _forward(
        client, "GET", f"/v2/notifications/{notification_id}", envelope=envelope
    )


@router.get(
    "/v2/notifications",
    summary="Get the status of multiple messages",
    operation_id="GetNotifications",
    responses={
        200: {
            "model": NotificationListResponse,
            "description": "One page of messages and their statuses",
        },
        **NOTIFY_ERRORS,
    },
    dependencies=[Authorised],
)
async def get_notifications(
    client: Client,
    envelope: Enveloped,
    status_: Annotated[
        NotificationStatus | None,
        Query(alias="status", description="Filter by delivery status."),
    ] = None,
    template_type: Annotated[
        TemplateType | None, Query(description="Filter by message type.")
    ] = None,
    reference: Annotated[str | None, Query(description="Filter by your own reference.")] = None,
    older_than: Annotated[
        UUID | None,
        Query(description="Return the next page: messages older than this notification ID."),
    ] = None,
    include_jobs: Annotated[
        bool | None,
        Query(description="Include messages sent from an uploaded CSV file."),
    ] = None,
) -> Response:
    return await _forward(
        client,
        "GET",
        "/v2/notifications",
        params={
            "status": status_,
            "template_type": template_type,
            "reference": reference,
            "older_than": None if older_than is None else str(older_than),
            "include_jobs": include_jobs,
        },
        envelope=envelope,
    )


@router.get(
    "/v2/notifications/{notification_id}/pdf",
    summary="Get a letter as a PDF",
    operation_id="GetLetterPdf",
    response_class=Response,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "The letter as a PDF"},
        **NOTIFY_ERRORS,
    },
    dependencies=[Authorised],
)
async def get_letter_pdf(notification_id: UUID, client: Client) -> Response:
    return await _forward(client, "GET", f"/v2/notifications/{notification_id}/pdf")


# --- received messages -------------------------------------------------------


@router.get(
    "/v2/received-text-messages",
    summary="Get received text messages",
    operation_id="GetReceivedTextMessages",
    responses={
        200: {
            "model": ReceivedTextMessageListResponse,
            "description": "One page of received messages",
        },
        **NOTIFY_ERRORS,
    },
    dependencies=[Authorised],
)
async def get_received_text_messages(
    client: Client,
    envelope: Enveloped,
    older_than: Annotated[
        UUID | None,
        Query(description="Return the next page: messages older than this message ID."),
    ] = None,
) -> Response:
    return await _forward(
        client,
        "GET",
        "/v2/received-text-messages",
        params={"older_than": None if older_than is None else str(older_than)},
        envelope=envelope,
    )


# --- templates ---------------------------------------------------------------


@router.get(
    "/v2/templates",
    summary="Get all templates",
    operation_id="GetTemplates",
    responses={
        200: {"model": TemplateListResponse, "description": "Every template on the service"},
        **NOTIFY_ERRORS,
    },
    dependencies=[Authorised],
)
async def get_templates(
    client: Client,
    envelope: Enveloped,
    type_: Annotated[
        TemplateType | None,
        Query(alias="type", description="Filter by template type."),
    ] = None,
) -> Response:
    return await _forward(
        client, "GET", "/v2/templates", params={"type": type_}, envelope=envelope
    )


@router.get(
    "/v2/template/{template_id}",
    summary="Get a template",
    operation_id="GetTemplate",
    responses={
        200: {"model": TemplateResponse, "description": "The template"},
        **NOTIFY_ERRORS,
    },
    dependencies=[Authorised],
)
async def get_template(template_id: UUID, client: Client, envelope: Enveloped) -> Response:
    return await _forward(
        client, "GET", f"/v2/template/{template_id}", envelope=envelope
    )


@router.get(
    "/v2/template/{template_id}/version/{version}",
    summary="Get a specific version of a template",
    operation_id="GetTemplateVersion",
    responses={
        200: {"model": TemplateResponse, "description": "That version of the template"},
        **NOTIFY_ERRORS,
    },
    dependencies=[Authorised],
)
async def get_template_version(
    template_id: UUID, version: int, client: Client, envelope: Enveloped
) -> Response:
    return await _forward(
        client, "GET", f"/v2/template/{template_id}/version/{version}", envelope=envelope
    )


@router.post(
    "/v2/template/{template_id}/preview",
    summary="Preview a template with personalisation applied",
    operation_id="PreviewTemplate",
    responses={
        200: {
            "model": TemplatePreviewResponse,
            "description": "The template with personalisation applied",
        },
        **NOTIFY_ERRORS,
    },
    dependencies=[Authorised],
)
async def preview_template(
    template_id: UUID, body: TemplatePreviewRequest, client: Client, envelope: Enveloped
) -> Response:
    return await _forward(
        client,
        "POST",
        f"/v2/template/{template_id}/preview",
        json=body.model_dump(exclude_none=True),
        envelope=envelope,
    )
