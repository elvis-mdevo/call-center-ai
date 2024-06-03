# First imports, to make sure the following logs are first
from helpers.logging import logger, APP_NAME, trace
from helpers.config import CONFIG


logger.info(f"{APP_NAME} v{CONFIG.version}")


# General imports
from typing import Any, Optional
from azure.communication.callautomation import (
    CallAutomationClient,
    PhoneNumberIdentifier,
)
from azure.core.credentials import AzureKeyCredential
from azure.core.messaging import CloudEvent
from azure.eventgrid import EventGridEvent, SystemEventNames
from helpers.pydantic_types.phone_numbers import PhoneNumber
from jinja2 import Environment, FileSystemLoader
from models.call import CallStateModel, CallGetModel, CallInitiateModel
from models.next import ActionEnum as NextActionEnum
from twilio.twiml.messaging_response import MessagingResponse
from urllib.parse import quote_plus, urljoin
from uuid import UUID
import asyncio
import mistune
from helpers.call_events import (
    on_call_connected,
    on_call_disconnected,
    on_end_call,
    on_ivr_recognized,
    on_new_call,
    on_play_completed,
    on_play_error,
    on_sms_received,
    on_speech_recognized,
    on_speech_timeout_error,
    on_speech_unknown_error,
    on_transfer_completed,
    on_transfer_error,
)
from helpers.call_utils import ContextEnum as CallContextEnum
from htmlmin.minify import html_minify
from http import HTTPStatus
from models.readiness import ReadinessModel, ReadinessCheckModel, ReadinessStatus
from pydantic import TypeAdapter, ValidationError
import azure.functions as func
import json
from os import getenv
from opentelemetry.semconv.trace import SpanAttributes


# Jinja configuration
_jinja = Environment(
    autoescape=True,
    enable_async=True,
    loader=FileSystemLoader("public_website"),
    optimized=False,  # Outsource optimization to html_minify
)
# Jinja custom functions
_jinja.filters["quote_plus"] = lambda x: quote_plus(str(x)) if x else ""
_jinja.filters["markdown"] = lambda x: mistune.create_markdown(plugins=["abbr", "speedup", "url"])(x) if x else ""  # type: ignore

# Azure Communication Services
_source_caller = PhoneNumberIdentifier(CONFIG.communication_services.phone_number)
logger.info(f"Using phone number {str(CONFIG.communication_services.phone_number)}")
# Cannot place calls with RBAC, need to use access key (see: https://learn.microsoft.com/en-us/azure/communication-services/concepts/authentication#authentication-options)
_automation_client = CallAutomationClient(
    endpoint=CONFIG.communication_services.endpoint,
    credential=AzureKeyCredential(
        CONFIG.communication_services.access_key.get_secret_value()
    ),
)

# Persistences
_cache = CONFIG.cache.instance()
_db = CONFIG.database.instance()
_search = CONFIG.ai_search.instance()
_sms = CONFIG.sms.instance()

# Azure Functions
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Communication Services callback
assert CONFIG.public_domain, "public_domain config is not set"
_COMMUNICATIONSERVICES_CALLABACK_TPL = urljoin(
    str(CONFIG.public_domain),
    "/communicationservices/event/{call_id}/{callback_secret}",
)
logger.info(f"Using call event URL {_COMMUNICATIONSERVICES_CALLABACK_TPL}")


@app.route(
    "health/liveness",
    methods=["GET"],
)
async def health_liveness_get(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(status_code=HTTPStatus.NO_CONTENT)


@app.route(
    "health/readiness",
    methods=["GET"],
)
async def health_readiness_get(req: func.HttpRequest) -> func.HttpResponse:
    # Check all components in parallel
    cache_check, db_check, search_check, sms_check = await asyncio.gather(
        _cache.areadiness(), _db.areadiness(), _search.areadiness(), _sms.areadiness()
    )
    readiness = ReadinessModel(
        status=ReadinessStatus.OK,
        checks=[
            ReadinessCheckModel(id="cache", status=cache_check),
            ReadinessCheckModel(id="index", status=db_check),
            ReadinessCheckModel(id="startup", status=ReadinessStatus.OK),
            ReadinessCheckModel(id="store", status=search_check),
            ReadinessCheckModel(id="stream", status=sms_check),
        ],
    )
    # If one of the checks fails, the whole readiness fails
    status_code = HTTPStatus.OK
    for check in readiness.checks:
        if check.status != ReadinessStatus.OK:
            readiness.status = ReadinessStatus.FAIL
            status_code = HTTPStatus.SERVICE_UNAVAILABLE
            break
    return func.HttpResponse(
        body=readiness.model_dump_json(indent=None),
        mimetype="application/json",
        status_code=status_code,
    )


@app.route(
    "report",
    methods=["GET"],
    trigger_arg_name="req",
)
async def report_get(req: func.HttpRequest) -> func.HttpResponse:
    try:
        phone_number = (
            PhoneNumber(req.params["phone_number"])
            if "phone_number" in req.params
            else None
        )
    except Exception as e:
        return _validation_error(e)
    count = 100
    calls, total = (
        await _db.call_asearch_all(
            count=count,
            phone_number=phone_number or None,
        )
        or []
    )
    template = _jinja.get_template("list.html.jinja")
    render = await template.render_async(
        applicationinsights_connection_string=getenv(
            "APPLICATIONINSIGHTS_CONNECTION_STRING"
        ),
        calls=calls or [],
        count=count,
        phone_number=phone_number,
        total=total,
        version=CONFIG.version,
    )
    render = html_minify(render)  # Minify HTML
    return func.HttpResponse(
        body=render,
        mimetype="text/html",
        status_code=HTTPStatus.OK,
    )


@app.route(
    "report/{call_id:guid}",
    methods=["GET"],
    trigger_arg_name="req",
)
async def report_single_get(req: func.HttpRequest) -> func.HttpResponse:
    try:
        call_id = UUID(req.route_params["call_id"])
    except Exception as e:
        return _validation_error(e)
    call = await _db.call_aget(call_id)
    if not call:
        return func.HttpResponse(
            body=f"Call {call_id} not found",
            mimetype="text/plain",
            status_code=HTTPStatus.NOT_FOUND,
        )
    template = _jinja.get_template("single.html.jinja")
    render = await template.render_async(
        applicationinsights_connection_string=getenv(
            "APPLICATIONINSIGHTS_CONNECTION_STRING"
        ),
        bot_company=call.initiate.bot_company,
        bot_name=call.initiate.bot_name,
        call=call,
        next_actions=[action for action in NextActionEnum],
        version=CONFIG.version,
    )
    render = html_minify(render)  # Minify HTML
    return func.HttpResponse(
        body=render,
        mimetype="text/html",
        status_code=HTTPStatus.OK,
    )


# TODO: Add total (int) and calls (list) as a wrapper for the list of calls
@app.route(
    "call/{phone_number}",
    methods=["GET"],
    trigger_arg_name="req",
)
async def call_search_get(req: func.HttpRequest) -> func.HttpResponse:
    try:
        phone_number = PhoneNumber(req.route_params["phone_number"])
    except Exception as e:
        return _validation_error(e)
    calls, _ = await _db.call_asearch_all(phone_number=phone_number, count=1)
    output = [CallGetModel.model_validate(call) for call in calls or []]
    return func.HttpResponse(
        body=TypeAdapter(list[CallGetModel]).dump_json(output),
        mimetype="application/json",
        status_code=HTTPStatus.OK,
    )


@app.route(
    "call/{call_id:guid}",
    methods=["GET"],
    trigger_arg_name="req",
)
async def call_get(req: func.HttpRequest) -> func.HttpResponse:
    try:
        call_id = UUID(req.route_params["call_id"])
    except Exception as e:
        return _validation_error(e)
    call = await _db.call_aget(call_id)
    if not call:
        return func.HttpResponse(
            body=f"Call {call_id} not found",
            mimetype="text/plain",
            status_code=HTTPStatus.NOT_FOUND,
        )
    return func.HttpResponse(
        body=call.model_dump_json(indent=None),
        mimetype="application/json",
        status_code=HTTPStatus.OK,
    )


@app.route(
    "call",
    methods=["POST"],
    trigger_arg_name="req",
)
async def call_post(req: func.HttpRequest) -> func.HttpResponse:
    try:
        initiate = CallInitiateModel.model_validate_json(req.get_body())
    except Exception as e:
        return _validation_error(e)
    url, call = await _communicationservices_event_url(initiate.phone_number, initiate)
    trace.get_current_span().set_attribute(
        SpanAttributes.ENDUSER_ID, call.initiate.phone_number
    )
    call_connection_properties = _automation_client.create_call(
        callback_url=url,
        cognitive_services_endpoint=CONFIG.cognitive_service.endpoint,
        source_caller_id_number=_source_caller,
        target_participant=PhoneNumberIdentifier(initiate.phone_number),  # type: ignore
    )
    logger.info(
        f"Created call with connection id: {call_connection_properties.call_connection_id}"
    )
    return func.HttpResponse(
        body=CallGetModel.model_validate(call).model_dump_json(indent=None),
        mimetype="application/json",
        status_code=HTTPStatus.CREATED,
    )


@app.queue_trigger(
    arg_name="call",
    connection="Storage",
    queue_name=CONFIG.communication_services.call_queue_name,
)
async def call_event(
    call: func.QueueMessage,
) -> None:
    event = EventGridEvent.from_json(call.get_body())
    event_type = event.event_type

    logger.debug(f"Call event with data {event.data}")
    if not event_type == SystemEventNames.AcsIncomingCallEventName:
        logger.warning(f"Event {event_type} not supported")
        return

    call_context: str = event.data["incomingCallContext"]
    phone_number = PhoneNumber(event.data["from"]["phoneNumber"]["value"])
    url, _call = await _communicationservices_event_url(phone_number)
    trace.get_current_span().set_attribute(
        SpanAttributes.ENDUSER_ID, _call.initiate.phone_number
    )
    await on_new_call(
        callback_url=url,
        client=_automation_client,
        incoming_context=call_context,
        phone_number=phone_number,
    )


@app.queue_trigger(
    arg_name="sms",
    connection="Storage",
    queue_name=CONFIG.communication_services.sms_queue_name,
)
@app.queue_output(
    arg_name="trainings",
    connection="Storage",
    queue_name=CONFIG.communication_services.trainings_queue_name,
)
@app.queue_output(
    arg_name="post",
    connection="Storage",
    queue_name=CONFIG.communication_services.post_queue_name,
)
async def sms_event(
    post: func.Out[str],
    sms: func.QueueMessage,
    trainings: func.Out[str],
) -> None:
    event = EventGridEvent.from_json(sms.get_body())
    event_type = event.event_type

    logger.debug(f"SMS event with data {event.data}")
    if not event_type == SystemEventNames.AcsSmsReceivedEventName:
        logger.warning(f"Event {event_type} not supported")
        return

    message: str = event.data["message"]
    phone_number: str = event.data["from"]
    call = await _db.call_asearch_one(phone_number)
    if not call:
        logger.warning(f"Call for phone number {phone_number} not found")
        return
    trace.get_current_span().set_attribute(
        SpanAttributes.ENDUSER_ID, call.initiate.phone_number
    )
    await on_sms_received(
        call=call,
        client=_automation_client,
        message=message,
        post_callback=lambda _call: _trigger_post_event(call=_call, post=post),
        trainings_callback=lambda _call: _trigger_trainings_event(
            call=_call, trainings=trainings
        ),
    )


@app.route(
    "communicationservices/event/{call_id:guid}/{secret:length(16)}",
    methods=["POST"],
    trigger_arg_name="req",
)
@app.queue_output(
    arg_name="trainings",
    connection="Storage",
    queue_name=CONFIG.communication_services.trainings_queue_name,
)
@app.queue_output(
    arg_name="post",
    connection="Storage",
    queue_name=CONFIG.communication_services.post_queue_name,
)
async def communicationservices_event_post(
    post: func.Out[str],
    req: func.HttpRequest,
    trainings: func.Out[str],
) -> func.HttpResponse:
    try:
        call_id = UUID(req.route_params["call_id"])
        secret: str = req.route_params["secret"]
    except Exception as e:
        return _validation_error(e)
    await asyncio.gather(
        *[
            _communicationservices_event_worker(
                call_id=call_id,
                event_dict=event_dict,
                post=post,
                secret=secret,
                trainings=trainings,
            )
            for event_dict in req.get_json()
        ]
    )
    return func.HttpResponse(status_code=HTTPStatus.NO_CONTENT)


async def _communicationservices_event_worker(
    call_id: UUID,
    event_dict: dict,
    post: func.Out[str],
    secret: str,
    trainings: func.Out[str],
) -> None:
    call = await _db.call_aget(call_id)
    if not call:
        logger.warning(f"Call {call_id} not found")
        return
    if call.callback_secret != secret:
        logger.warning(f"Secret for call {call_id} does not match")
        return

    trace.get_current_span().set_attribute(
        SpanAttributes.ENDUSER_ID, call.initiate.phone_number
    )
    event = CloudEvent.from_dict(event_dict)
    assert isinstance(event.data, dict)

    # Store connection ID
    connection_id = event.data["callConnectionId"]
    call.voice_id = connection_id
    # Extract context
    event_type = event.type
    # Extract event context
    operation_context = event.data.get("operationContext", None)
    operation_contexts: Optional[list[CallContextEnum]] = (
        [CallContextEnum(context) for context in json.loads(operation_context)]
        if operation_context
        else None
    )

    logger.debug(f"Call event received {event_type} for call {call}")
    logger.debug(event.data)

    if event_type == "Microsoft.Communication.CallConnected":  # Call answered
        await on_call_connected(
            call=call,
            client=_automation_client,
        )

    elif event_type == "Microsoft.Communication.CallDisconnected":  # Call hung up
        await on_call_disconnected(
            call=call,
            client=_automation_client,
            post_callback=lambda _call: _trigger_post_event(call=_call, post=post),
        )

    elif (
        event_type == "Microsoft.Communication.RecognizeCompleted"
    ):  # Speech recognized
        recognition_result: str = event.data["recognitionType"]

        if recognition_result == "speech":  # Handle voice
            speech_text: Optional[str] = event.data["speechResult"]["speech"]
            if speech_text:
                await on_speech_recognized(
                    call=call,
                    client=_automation_client,
                    text=speech_text,
                    post_callback=lambda _call: _trigger_post_event(
                        call=_call, post=post
                    ),
                    trainings_callback=lambda _call: _trigger_trainings_event(
                        call=_call, trainings=trainings
                    ),
                )

        elif recognition_result == "choices":  # Handle IVR
            label_detected: str = event.data["choiceResult"]["label"]
            await on_ivr_recognized(
                call=call,
                client=_automation_client,
                label=label_detected,
                post_callback=lambda _call: _trigger_post_event(call=_call, post=post),
                trainings_callback=lambda _call: _trigger_trainings_event(
                    call=_call, trainings=trainings
                ),
            )

    elif (
        event_type == "Microsoft.Communication.RecognizeFailed"
    ):  # Speech recognition failed
        result_information = event.data["resultInformation"]
        error_code: int = result_information["subCode"]

        # Error codes:
        # 8510 = Action failed, initial silence timeout reached
        # 8532 = Action failed, inter-digit silence timeout reached
        # See: https://github.com/MicrosoftDocs/azure-docs/blob/main/articles/communication-services/how-tos/call-automation/recognize-action.md#event-codes
        if error_code in (8510, 8532):  # Timeout retry
            await on_speech_timeout_error(
                call=call,
                client=_automation_client,
                contexts=operation_contexts,
            )
        else:  # Unknown error
            await on_speech_unknown_error(
                call=call,
                client=_automation_client,
                error_code=error_code,
            )

    elif event_type == "Microsoft.Communication.PlayCompleted":  # Media played
        await on_play_completed(
            call=call,
            client=_automation_client,
            contexts=operation_contexts,
            post_callback=lambda _call: _trigger_post_event(call=_call, post=post),
        )

    elif event_type == "Microsoft.Communication.PlayFailed":  # Media play failed
        result_information = event.data["resultInformation"]
        error_code: int = result_information["subCode"]
        await on_play_error(error_code)

    elif (
        event_type == "Microsoft.Communication.CallTransferAccepted"
    ):  # Call transfer accepted
        await on_transfer_completed()

    elif (
        event_type == "Microsoft.Communication.CallTransferFailed"
    ):  # Call transfer failed
        result_information = event.data["resultInformation"]
        sub_code: int = result_information["subCode"]
        await on_transfer_error(
            call=call,
            client=_automation_client,
            error_code=sub_code,
        )

    await _db.call_aset(
        call
    )  # TODO: Do not persist on every event, this is simpler but not efficient


@app.queue_trigger(
    arg_name="trainings",
    connection="Storage",
    queue_name=CONFIG.communication_services.trainings_queue_name,
)
async def trainings_event(
    trainings: func.QueueMessage,
) -> None:
    call = CallStateModel.model_validate_json(trainings.get_body())
    logger.debug(f"Trainings event received for call {call}")
    trace.get_current_span().set_attribute(
        SpanAttributes.ENDUSER_ID, call.initiate.phone_number
    )
    await call.trainings()  # Get trainings by advance to populate cache


@app.queue_trigger(
    arg_name="post",
    connection="Storage",
    queue_name=CONFIG.communication_services.post_queue_name,
)
async def post_event(
    post: func.QueueMessage,
) -> None:
    call = CallStateModel.model_validate_json(post.get_body())
    logger.debug(f"Post event received for call {call}")
    trace.get_current_span().set_attribute(
        SpanAttributes.ENDUSER_ID, call.initiate.phone_number
    )
    await on_end_call(call)


def _trigger_trainings_event(
    call: CallStateModel,
    trainings: func.Out[str],
) -> None:
    """
    Shortcut to add trainings to the queue.
    """
    trainings.set(call.model_dump_json(indent=None))


def _trigger_post_event(
    call: CallStateModel,
    post: func.Out[str],
) -> None:
    """
    Shortcut to add post-call intelligence to the queue.
    """
    post.set(call.model_dump_json(indent=None))


async def _communicationservices_event_url(
    phone_number: PhoneNumber, initiate: Optional[CallInitiateModel] = None
) -> tuple[str, CallStateModel]:
    """
    Generate the callback URL for a call.

    If the caller has already called, use the same call ID, to keep the conversation history. Otherwise, create a new call ID.
    """
    call = await _db.call_asearch_one(phone_number)
    if not call or (
        initiate and call.initiate != initiate
    ):  # Create new call if initiate is different
        call = CallStateModel(
            initiate=initiate
            or CallInitiateModel(
                **CONFIG.workflow.initiate.model_dump(),
                phone_number=phone_number,
            )
        )
        await _db.call_aset(call)  # Create for the first time
    url = _COMMUNICATIONSERVICES_CALLABACK_TPL.format(
        callback_secret=call.callback_secret,
        call_id=str(call.call_id),
    )
    return url, call


# TODO: Secure this endpoint with a secret, either in the Authorization header or in the URL
@app.route(
    "twilio/sms",
    methods=["POST"],
    trigger_arg_name="req",
)
@app.queue_output(
    arg_name="trainings",
    connection="Storage",
    queue_name=CONFIG.communication_services.trainings_queue_name,
)
@app.queue_output(
    arg_name="post",
    connection="Storage",
    queue_name=CONFIG.communication_services.post_queue_name,
)
async def twilio_sms_post(
    post: func.Out[str],
    req: func.HttpRequest,
    trainings: func.Out[str],
) -> func.HttpResponse:
    """
    Handle incoming SMS event from Twilio.
    """
    if not req.form:
        return _validation_error(Exception("No form data"))
    try:
        phone_number = PhoneNumber(req.form["From"])
        message: str = req.form["Body"]
    except Exception as e:
        return _validation_error(e)
    call = await _db.call_asearch_one(phone_number)
    if not call:
        logger.warning(f"Call for phone number {phone_number} not found")
    else:
        trace.get_current_span().set_attribute(
            SpanAttributes.ENDUSER_ID, call.initiate.phone_number
        )
        event_status = await on_sms_received(
            call=call,
            message=message,
            client=_automation_client,
            post_callback=lambda _call: _trigger_post_event(call=_call, post=post),
            trainings_callback=lambda _call: _trigger_trainings_event(
                call=_call, trainings=trainings
            ),
        )
        if not event_status:
            return func.HttpResponse(status_code=HTTPStatus.INTERNAL_SERVER_ERROR)
    return func.HttpResponse(
        body=str(MessagingResponse()),  # Twilio expects an empty response everytime
        mimetype="application/xml",
        status_code=HTTPStatus.OK,
    )


def _validation_error(
    e: Exception,
) -> func.HttpResponse:
    body: dict[str, Any] = {
        "error": {
            "message": "Validation error",
            "details": [],
        }
    }
    if isinstance(e, ValidationError):
        body["error"][
            "details"
        ] = e.errors()  # Pydantic returns well formatted errors, use them
    elif isinstance(e, ValueError):
        body["error"]["details"] = str(
            e
        )  # TODO: Could it expose sensitive information?
    return func.HttpResponse(
        body=json.dumps(body),
        mimetype="application/json",
        status_code=HTTPStatus.BAD_REQUEST,
    )
