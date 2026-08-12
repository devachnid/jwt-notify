"""Request models for the Notify proxy routes.

Field names and requirements follow the published Notify OpenAPI specification
(https://github.com/alphagov/notifications-tech-docs/tree/main/openapi).

Only the request side is modelled. Responses are passed through from Notify
verbatim so that new fields appear without a change here; their shape is
documented in the Swagger definition used by the Azure custom connector.
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
