"""Request and response models for the Notify proxy routes.

Field names and requirements follow the published Notify OpenAPI specification
(https://github.com/alphagov/notifications-tech-docs/tree/main/openapi) and the
REST API documentation (https://docs.notifications.service.gov.uk/rest-api.html).

Response bodies are still passed through from Notify verbatim — the models are
there to describe them, not to validate or reshape them, and every one accepts
unknown fields so that a new field from Notify reaches the caller untouched.
Describing them matters for the Azure custom connector: the Logic App designer
builds its output fields from the response schema, so an operation documented
with an empty schema offers nothing to pick from.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Personalisation = dict[str, Any]

#: Delivery statuses accepted by GET /v2/notifications.
NotificationStatus = Literal[
    "accepted",
    "created",
    "sending",
    "pending",
    "sent",
    "received",
    "delivered",
    "cancelled",
    "pending-virus-check",
    "virus-scan-failed",
    "validation-failed",
    "permanent-failure",
    "temporary-failure",
    "technical-failure",
]

TemplateType = Literal["email", "sms", "letter"]

_PERSONALISATION_DESCRIPTION = (
    "Values for the template's placeholders. Values may be strings, numbers, "
    "or a list (rendered as bullet points). To attach a file, pass an object "
    "with a base64 'file' key, plus optional 'filename', "
    "'confirm_email_before_download' and 'retention_period'."
)

_REFERENCE_DESCRIPTION = (
    "Your own identifier for a notification or batch of notifications. Must "
    "not contain personal information."
)


class _NotifyRequest(BaseModel):
    # Notify adds request fields over time; accept and forward anything extra
    # rather than rejecting a request this service has not been taught about.
    model_config = ConfigDict(extra="allow")


class SmsRequest(_NotifyRequest):
    phone_number: str = Field(
        ...,
        min_length=7,
        max_length=15,
        description="Phone number of the recipient. UK or international.",
        examples=["+447900900123"],
    )
    template_id: str = Field(
        ...,
        description="ID of the text message template.",
        examples=["f33517ff-2a88-4f6e-b855-c550268ce08a"],
    )
    personalisation: Personalisation | None = Field(
        None, description=_PERSONALISATION_DESCRIPTION
    )
    reference: str | None = Field(None, description=_REFERENCE_DESCRIPTION)
    sms_sender_id: str | None = Field(
        None,
        description=(
            "ID of a text message sender configured on your service. Leave out "
            "to use the default sender."
        ),
    )


class EmailRequest(_NotifyRequest):
    email_address: str = Field(
        ...,
        min_length=5,
        max_length=250,
        description="Email address of the recipient.",
    )
    template_id: str = Field(..., description="ID of the email template.")
    personalisation: Personalisation | None = Field(
        None, description=_PERSONALISATION_DESCRIPTION
    )
    reference: str | None = Field(None, description=_REFERENCE_DESCRIPTION)
    email_reply_to_id: str | None = Field(
        None,
        description=(
            "ID of a reply-to address configured on your service. Leave out to "
            "use the default."
        ),
    )
    one_click_unsubscribe_url: str | None = Field(
        None,
        description=(
            "URL that unsubscribes the recipient in response to an empty POST. "
            "Required for subscription emails."
        ),
    )
    sanitise_content_for: list[str] | None = Field(
        None,
        description=(
            "Names of personalisation fields whose content Notify should "
            "sanitise: Markdown is escaped and URLs are removed."
        ),
    )


class LetterRequest(_NotifyRequest):
    """A letter built from a template."""

    template_id: str = Field(..., description="ID of the letter template.")
    personalisation: Personalisation = Field(
        ...,
        description=(
            "The recipient's address plus any template placeholders. The "
            "address needs at least three lines: address_line_1 and "
            "address_line_2 must contain alphanumeric characters, and the last "
            "line used must be a UK postcode or a country outside the UK."
        ),
    )
    reference: str | None = Field(None, description=_REFERENCE_DESCRIPTION)


class PrecompiledLetterRequest(_NotifyRequest):
    """A letter supplied as a ready-made PDF.

    Notify accepts this on the same endpoint as a templated letter; it is a
    separate operation here so that each has a single, unambiguous schema for
    the Azure custom connector.
    """

    reference: str = Field(..., description=_REFERENCE_DESCRIPTION)
    content: str = Field(..., description="The letter as a base64 encoded PDF.")
    postage: str | None = Field(
        None, description="Postage class, for example 'first' or 'second'."
    )


class TemplatePreviewRequest(_NotifyRequest):
    personalisation: Personalisation | None = Field(
        None, description=_PERSONALISATION_DESCRIPTION
    )


# --- responses ---------------------------------------------------------------


class _NotifyResponse(BaseModel):
    # Notify adds response fields over time, and the proxy passes the body
    # through as it arrives; the model only describes what is known today.
    model_config = ConfigDict(extra="allow")


class NotifyErrorDetail(_NotifyResponse):
    error: str = Field(
        ..., description="Error class, for example 'BadRequestError' or 'ValidationError'."
    )
    message: str = Field(..., description="What went wrong, in words.")


class NotifyError(_NotifyResponse):
    """The body Notify returns with a 400, 403, 429 or 500.

    Documented so that a Logic App can read the reason a send was rejected
    rather than only seeing that the action failed.
    """

    status_code: int = Field(..., description="HTTP status code, repeated in the body.")
    errors: list[NotifyErrorDetail] = Field(
        ..., description="One entry per problem found with the request."
    )


class TemplateSummary(_NotifyResponse):
    id: str = Field(..., description="ID of the template used.")
    version: int = Field(..., description="Version of the template used.")
    uri: str = Field(..., description="URL of that version of the template.")


class SmsContent(_NotifyResponse):
    body: str = Field(..., description="Text message as it was sent.")
    from_number: str | None = Field(None, description="Sender the message came from.")


class SmsResponse(_NotifyResponse):
    """Notify's 201 response to a text message send."""

    id: str = Field(..., description="ID of the notification. Use it to check delivery.")
    reference: str | None = Field(None, description=_REFERENCE_DESCRIPTION)
    uri: str = Field(..., description="URL of the notification on the Notify API.")
    template: TemplateSummary
    content: SmsContent
    scheduled_for: str | None = Field(
        None, description="When the message is scheduled to be sent, if it was scheduled."
    )


class EmailContent(_NotifyResponse):
    body: str = Field(..., description="Email body as it was sent.")
    subject: str = Field(..., description="Subject line as it was sent.")
    from_email: str | None = Field(None, description="Address the email came from.")
    one_click_unsubscribe_url: str | None = Field(
        None, description="Unsubscribe URL included in the email, if one was supplied."
    )


class EmailResponse(_NotifyResponse):
    """Notify's 201 response to an email send."""

    id: str = Field(..., description="ID of the notification. Use it to check delivery.")
    reference: str | None = Field(None, description=_REFERENCE_DESCRIPTION)
    uri: str = Field(..., description="URL of the notification on the Notify API.")
    template: TemplateSummary
    content: EmailContent
    scheduled_for: str | None = Field(
        None, description="When the email is scheduled to be sent, if it was scheduled."
    )


class LetterContent(_NotifyResponse):
    body: str = Field(..., description="Letter text as it was sent.")
    subject: str = Field(..., description="Subject of the letter template.")


class LetterResponse(_NotifyResponse):
    """Notify's 201 response to a templated letter send."""

    id: str = Field(..., description="ID of the notification. Use it to check delivery.")
    reference: str | None = Field(None, description=_REFERENCE_DESCRIPTION)
    uri: str = Field(..., description="URL of the notification on the Notify API.")
    template: TemplateSummary
    content: LetterContent
    scheduled_for: str | None = Field(
        None, description="When the letter is scheduled to be sent, if it was scheduled."
    )


class PrecompiledLetterResponse(_NotifyResponse):
    """Notify's 201 response to a precompiled letter send."""

    id: str = Field(..., description="ID of the notification. Use it to check delivery.")
    reference: str = Field(..., description=_REFERENCE_DESCRIPTION)
    postage: str | None = Field(None, description="Postage class the letter was sent at.")


class NotificationResponse(_NotifyResponse):
    """One message and its delivery status."""

    id: str = Field(..., description="ID of the notification.")
    reference: str | None = Field(None, description=_REFERENCE_DESCRIPTION)
    type: str | None = Field(None, description="'sms', 'email' or 'letter'.")
    status: str | None = Field(
        None, description="Delivery status, for example 'delivered' or 'permanent-failure'."
    )
    template: TemplateSummary | None = None
    body: str | None = Field(None, description="Message body as it was sent.")
    subject: str | None = Field(None, description="Subject line, for emails and letters.")
    phone_number: str | None = Field(None, description="Recipient, for text messages.")
    email_address: str | None = Field(None, description="Recipient, for emails.")
    line_1: str | None = Field(None, description="Address line 1, for letters.")
    line_2: str | None = Field(None, description="Address line 2, for letters.")
    line_3: str | None = Field(None, description="Address line 3, for letters.")
    line_4: str | None = Field(None, description="Address line 4, for letters.")
    line_5: str | None = Field(None, description="Address line 5, for letters.")
    line_6: str | None = Field(None, description="Address line 6, for letters.")
    postcode: str | None = Field(None, description="Postcode, for letters.")
    postage: str | None = Field(None, description="Postage class, for letters.")
    created_at: str | None = Field(None, description="When the message was created.")
    sent_at: str | None = Field(None, description="When Notify sent the message.")
    completed_at: str | None = Field(
        None, description="When the message reached a final status."
    )
    created_by_name: str | None = Field(
        None, description="Who sent the message, if it was sent from the Notify website."
    )
    scheduled_for: str | None = Field(
        None, description="When the message is scheduled to be sent, if it was scheduled."
    )


class Links(_NotifyResponse):
    current: str | None = Field(None, description="URL of this page of results.")
    next: str | None = Field(None, description="URL of the next page, if there is one.")


class NotificationListResponse(_NotifyResponse):
    notifications: list[NotificationResponse] = Field(
        ..., description="Up to 250 messages, newest first."
    )
    links: Links | None = None


class ReceivedTextMessage(_NotifyResponse):
    id: str = Field(..., description="ID of the received message.")
    created_at: str | None = Field(None, description="When the message was received.")
    service_id: str | None = Field(None, description="ID of the receiving Notify service.")
    notify_number: str | None = Field(None, description="Number the message was sent to.")
    user_number: str | None = Field(None, description="Number the message came from.")
    content: str | None = Field(None, description="Text of the message.")


class ReceivedTextMessageListResponse(_NotifyResponse):
    received_text_messages: list[ReceivedTextMessage] = Field(
        ..., description="Up to 250 received messages, newest first."
    )
    links: Links | None = None


class TemplateResponse(_NotifyResponse):
    id: str = Field(..., description="ID of the template.")
    name: str | None = Field(None, description="Name of the template.")
    type: str | None = Field(None, description="'sms', 'email' or 'letter'.")
    version: int | None = Field(None, description="Version of the template.")
    body: str | None = Field(None, description="Template body, placeholders unfilled.")
    subject: str | None = Field(None, description="Subject line, for emails and letters.")
    created_at: str | None = Field(None, description="When the template was created.")
    updated_at: str | None = Field(None, description="When the template was last changed.")
    created_by: str | None = Field(None, description="Who created the template.")
    letter_contact_block: str | None = Field(
        None, description="Contact block printed on letters."
    )


class TemplateListResponse(_NotifyResponse):
    templates: list[TemplateResponse] = Field(
        ..., description="Every template on the service."
    )


class TemplatePreviewResponse(_NotifyResponse):
    id: str = Field(..., description="ID of the template.")
    type: str | None = Field(None, description="'sms', 'email' or 'letter'.")
    version: int | None = Field(None, description="Version of the template.")
    body: str = Field(..., description="Template body with personalisation applied.")
    subject: str | None = Field(None, description="Subject line, for emails and letters.")
    html: str | None = Field(None, description="HTML version of the body, for emails.")


class NotifyEnvelope(BaseModel):
    """Notify's answer wrapped up, for callers that cannot read a failed call.

    Returned with HTTP 200 whatever Notify said, so that a Logic App action
    never fails on a rejected send and can read the status code and the error
    detail as ordinary fields instead. Requested with the ``X-Notify-Envelope``
    header; the connector built with ``--envelope`` sends it on every call.
    """

    status_code: int = Field(
        ..., description="Status code Notify returned, for example 201 or 400."
    )
    success: bool = Field(..., description="True when Notify accepted the request.")
    body: dict[str, Any] | None = Field(
        None, description="The body Notify returned, whether it accepted the request or not."
    )
    errors: list[NotifyErrorDetail] = Field(
        default_factory=list,
        description="Reasons the request was rejected. Empty when it succeeded.",
    )


class HealthResponse(BaseModel):
    status: str = Field(..., description="'ok' when the service is up.")
    proxy_enabled: bool = Field(
        ..., description="Whether a Notify API key is configured for the /v2 routes."
    )
