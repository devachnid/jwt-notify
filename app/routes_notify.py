"""Proxy routes mirroring the GOV.UK Notify REST API.

Paths match Notify's own, so a client built against these routes reads exactly
like one built against Notify — the only difference being that this service
supplies the JWT.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response

from .models import (
    EmailRequest,
    LetterRequest,
    NotificationStatus,
    PrecompiledLetterRequest,
    SmsRequest,
    TemplatePreviewRequest,
    TemplateType,
)
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


async def require_proxy_key(
    request: Request,
    x_proxy_key: Annotated[str | None, Header()] = None,
) -> None:
    """Check the shared secret, when one is configured.

    With no ``PROXY_KEY`` set the service trusts its front end (for example
    Cloudflare Access) to have authenticated the caller already.
    """
    expected: str | None = request.app.state.settings.proxy_key
    if expected is None:
        return
    import hmac

    if x_proxy_key is None or not hmac.compare_digest(x_proxy_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Proxy-Key header",
        )


Client = Annotated[NotifyClient, Depends(get_client)]
Authorised = Depends(require_proxy_key)


def _passthrough(response: httpx.Response) -> Response:
    """Return Notify's response as our own.

    Status code and body are preserved so that callers see Notify's own
    validation errors and rate-limit responses rather than a rewrite of them.
    """
    headers = {}
    if "retry-after" in response.headers:
        headers["Retry-After"] = response.headers["retry-after"]

    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            return JSONResponse(
                status_code=response.status_code, content=response.json(), headers=headers
            )
        except ValueError:
            pass

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
) -> Response:
    try:
        upstream = await client.request(method, path, params=params, json=json)
    except NotifyUnavailableError as exc:
        raise HTTPException(
            status_code=(
                status.HTTP_504_GATEWAY_TIMEOUT if exc.timeout else status.HTTP_502_BAD_GATEWAY
            ),
            detail=str(exc),
        ) from exc
    return _passthrough(upstream)


# --- sending -----------------------------------------------------------------


@router.post(
    "/v2/notifications/sms",
    summary="Send a text message",
    operation_id="SendSms",
    dependencies=[Authorised],
)
async def send_sms(body: SmsRequest, client: Client) -> Response:
    return await _forward(
        client, "POST", "/v2/notifications/sms", json=body.model_dump(exclude_none=True)
    )


@router.post(
    "/v2/notifications/email",
    summary="Send an email",
    operation_id="SendEmail",
    dependencies=[Authorised],
)
async def send_email(body: EmailRequest, client: Client) -> Response:
    return await _forward(
        client, "POST", "/v2/notifications/email", json=body.model_dump(exclude_none=True)
    )


@router.post(
    "/v2/notifications/letter",
    summary="Send a letter from a template",
    operation_id="SendLetter",
    dependencies=[Authorised],
)
async def send_letter(body: LetterRequest, client: Client) -> Response:
    return await _forward(
        client, "POST", "/v2/notifications/letter", json=body.model_dump(exclude_none=True)
    )


@router.post(
    "/v2/notifications/letter/precompiled",
    summary="Send a precompiled letter (a ready-made PDF)",
    operation_id="SendPrecompiledLetter",
    dependencies=[Authorised],
)
async def send_precompiled_letter(
    body: PrecompiledLetterRequest, client: Client
) -> Response:
    # Notify takes both letter kinds on /v2/notifications/letter, distinguished
    # by the body. They are split here so each connector operation has one
    # schema instead of a oneOf, which Swagger 2.0 cannot express.
    return await _forward(
        client, "POST", "/v2/notifications/letter", json=body.model_dump(exclude_none=True)
    )


# --- message status ----------------------------------------------------------


@router.get(
    "/v2/notifications/{notification_id}",
    summary="Get the status of one message",
    operation_id="GetNotification",
    dependencies=[Authorised],
)
async def get_notification(notification_id: str, client: Client) -> Response:
    return await _forward(client, "GET", f"/v2/notifications/{notification_id}")


@router.get(
    "/v2/notifications",
    summary="Get the status of multiple messages",
    operation_id="GetNotifications",
    dependencies=[Authorised],
)
async def get_notifications(
    client: Client,
    status_: Annotated[
        NotificationStatus | None,
        Query(alias="status", description="Filter by delivery status."),
    ] = None,
    template_type: Annotated[
        TemplateType | None, Query(description="Filter by message type.")
    ] = None,
    reference: Annotated[str | None, Query(description="Filter by your own reference.")] = None,
    older_than: Annotated[
        str | None,
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
            "older_than": older_than,
            "include_jobs": include_jobs,
        },
    )


@router.get(
    "/v2/notifications/{notification_id}/pdf",
    summary="Get a letter as a PDF",
    operation_id="GetLetterPdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
    dependencies=[Authorised],
)
async def get_letter_pdf(notification_id: str, client: Client) -> Response:
    return await _forward(client, "GET", f"/v2/notifications/{notification_id}/pdf")


# --- received messages -------------------------------------------------------


@router.get(
    "/v2/received-text-messages",
    summary="Get received text messages",
    operation_id="GetReceivedTextMessages",
    dependencies=[Authorised],
)
async def get_received_text_messages(
    client: Client,
    older_than: Annotated[
        str | None,
        Query(description="Return the next page: messages older than this message ID."),
    ] = None,
) -> Response:
    return await _forward(
        client, "GET", "/v2/received-text-messages", params={"older_than": older_than}
    )


# --- templates ---------------------------------------------------------------


@router.get(
    "/v2/templates",
    summary="Get all templates",
    operation_id="GetTemplates",
    dependencies=[Authorised],
)
async def get_templates(
    client: Client,
    type_: Annotated[
        TemplateType | None,
        Query(alias="type", description="Filter by template type."),
    ] = None,
) -> Response:
    return await _forward(client, "GET", "/v2/templates", params={"type": type_})


@router.get(
    "/v2/template/{template_id}",
    summary="Get a template",
    operation_id="GetTemplate",
    dependencies=[Authorised],
)
async def get_template(template_id: str, client: Client) -> Response:
    return await _forward(client, "GET", f"/v2/template/{template_id}")


@router.get(
    "/v2/template/{template_id}/version/{version}",
    summary="Get a specific version of a template",
    operation_id="GetTemplateVersion",
    dependencies=[Authorised],
)
async def get_template_version(template_id: str, version: int, client: Client) -> Response:
    return await _forward(client, "GET", f"/v2/template/{template_id}/version/{version}")


@router.post(
    "/v2/template/{template_id}/preview",
    summary="Preview a template with personalisation applied",
    operation_id="PreviewTemplate",
    dependencies=[Authorised],
)
async def preview_template(
    template_id: str, body: TemplatePreviewRequest, client: Client
) -> Response:
    return await _forward(
        client,
        "POST",
        f"/v2/template/{template_id}/preview",
        json=body.model_dump(exclude_none=True),
    )
