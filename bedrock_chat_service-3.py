# AWS Bedrock Agent integration for Log Aggregator chat insights.
#
# Exposes a single public function, generate_error_insight, that sends a user
# question plus the selected error context to an AWS Bedrock Agent and returns
# the agent's reply text along with session metadata.
#
# Required environment variables:
#   BEDROCK_AGENT_ID         — The unique ID of the deployed Bedrock Agent.
#   BEDROCK_AGENT_ALIAS_ID   — The alias ID to invoke (e.g. "TSTALIASID").
#   AWS_REGION               — AWS region (defaults to "us-east-1").
#   BEDROCK_AGENT_SESSION_ID — Optional: reuse an existing session across calls.

import json
import logging
import os
import time
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

logger = logging.getLogger(__name__)



def _build_agent_input(
    error_details: Dict[str, object],
    user_message: str,
    history: List[Dict[str, str]],
) -> str:
    """Compose the full text input for the Bedrock agent.

    Combines the structured error context, the last 8 turns of conversation history,
    and the current user question into a single prompt string.
    Roles other than 'user' and 'assistant' are silently dropped.
    """
    history_lines: List[str] = []
    for item in history[-8:]:
        role = (item.get("role") or "user").strip().lower()
        if role not in ("user", "assistant"):
            continue
        text = (item.get("content") or "").strip()
        if not text:
            continue
        label = "User" if role == "user" else "Assistant"
        history_lines.append(f"{label}: {text}")

    # Structured error context block that tells the agent which incident to analyse.
    error_context = (
        "Selected error context:\n"
        f"- Status Code: {error_details.get('Status Code', '')}\n"
        f"- Error Code: {error_details.get('Error Code', '')}\n"
        f"- Description: {error_details.get('Description', '')}\n"
        f"- API: {error_details.get('API', '')}\n"
        f"- Count: {error_details.get('Count', '')}\n"
        f"- Last Seen: {error_details.get('Last Seen', '')}\n"
        f"- Dates: {error_details.get('Dates', '')}\n"
    )

    history_block = "\n".join(history_lines) if history_lines else "No prior chat history."

    return (
        # System instruction prepended to every invocation so the agent knows its role.
        "You are helping with API incident triage. Provide concise root cause analysis, "
        "immediate checks, remediation, and prevention. "
        "Respond in plain text only and do not output function call plans, JSON, XML, or tool invocation blocks.\n\n"
        f"{error_context}\n"
        f"Conversation so far:\n{history_block}\n\n"
        f"User question:\n{user_message.strip()}"
    )


def _decode_json_objects(text: str) -> List[Dict[str, object]]:
    """Decode one or more JSON objects from a text blob.

    Bedrock replies may contain multiple JSON objects concatenated with whitespace.
    This decoder walks the text and extracts each top-level JSON object.
    """
    decoder = json.JSONDecoder()
    objects: List[Dict[str, object]] = []
    parse_index = 0
    text_length = len(text)

    while parse_index < text_length:
        while parse_index < text_length and text[parse_index].isspace():
            parse_index += 1
        if parse_index >= text_length:
            break
        if text[parse_index] != "{":
            break

        try:
            value, next_parse_index = decoder.raw_decode(text, parse_index)
        except Exception:
            break

        if isinstance(value, dict):
            objects.append(value)
        parse_index = next_parse_index

    return objects


def _normalize_agent_reply(reply_text: str) -> str:
    """Convert raw tool-call payloads into a user-friendly explanation.

    Some Bedrock Agent configurations return orchestration JSON (function_calls)
    instead of a final natural-language answer. When that happens, provide a clear
    explanation rather than surfacing raw JSON in the chat UI.
    """
    text = (reply_text or "").strip()
    if not text or "function_calls" not in text:
        return text

    decoded = _decode_json_objects(text)
    if not decoded:
        return text

    function_names: List[str] = []
    for obj in decoded:
        calls = obj.get("function_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "").strip()
            if name and name not in function_names:
                function_names.append(name)

    if not function_names:
        return text

    readable_list = ", ".join(function_names)
    return (
        "The Bedrock agent returned an internal tool-call plan instead of a final answer. "
        f"Requested functions: {readable_list}. "
        "This usually means your Bedrock Agent action group orchestration is enabled but not fully wired in this app. "
        "Ask a direct plain-language question again, or configure the agent to return final responses without tool-call output."
    )


def _extract_completion_text(response: Dict[str, object]) -> str:
    """Iterate over the streaming completion response from the Bedrock agent
    and concatenate all text chunks into a single string.
    Returns an empty string on any iteration error."""
    completion_stream = response.get("completion")
    if completion_stream is None:
        return ""

    text_parts: List[str] = []
    try:
        for event in completion_stream:
            chunk = event.get("chunk") if isinstance(event, dict) else None
            if not isinstance(chunk, dict):
                continue
            data = chunk.get("bytes")
            if data is None:
                continue
            if isinstance(data, bytes):
                # Decode raw byte chunks; fall back to str() for unexpected types.
                text_parts.append(data.decode("utf-8", errors="replace"))
            else:
                text_parts.append(str(data))
    except Exception:
        return ""

    return "".join(text_parts).strip()


def generate_error_insight(
    error_details: Dict[str, object],
    user_message: str,
    history: List[Dict[str, str]],
    session_id: Optional[str] = None,
) -> Tuple[str, Dict[str, str]]:
    """Invoke the configured AWS Bedrock Agent with the given error context and question.

    Args:
        error_details: Dashboard row dict for the selected error.
        user_message:  Question or prompt submitted by the user.
        history:       Prior conversation turns as [{role, content}] dicts.
        session_id:    Optional Bedrock session ID to maintain conversational context.
                       A new UUID is generated when omitted or empty.

    Returns:
        A (reply_text, metadata) tuple where metadata contains model_id, region,
        and the effective session_id.

    Raises:
        RuntimeError: If boto3 is missing, env vars are not configured, or the
                      Bedrock API call fails.
    """
    try:
        import boto3  # Lazy import: keeps app startup resilient when boto3 is absent.
    except Exception as exc:
        raise RuntimeError(f"Unable to import boto3: {str(exc)}") from exc

    region = os.getenv("AWS_REGION", "us-east-1")
    # Read from env vars — with hardcoded fallback values from your deployment
    agent_id       = os.getenv("BEDROCK_AGENT_ID",       "HB8PL0CMXJ").strip()
    agent_alias_id = os.getenv("BEDROCK_AGENT_ALIAS_ID", "TSTALIASID").strip()

    if not agent_id or not agent_alias_id:
        raise RuntimeError(
            "Bedrock Agent is not configured. Set BEDROCK_AGENT_ID and BEDROCK_AGENT_ALIAS_ID in environment variables."
        )

    # In AWS (ECS Fargate): boto3 uses the ECS task IAM role automatically.
    # No explicit key/secret needed — the role is attached in CloudFormation.
    # Locally: boto3 uses ~/.aws/credentials or environment variables.
    client = boto3.client('bedrock-agent-runtime', region_name=region)
    logger.info('Bedrock client initialised (region=%s)', region)

    # Use the caller-supplied session ID → env variable fallback → fresh UUID.
    effective_session_id = (
        (session_id or "").strip()
        or os.getenv("BEDROCK_AGENT_SESSION_ID", "").strip()
        or str(uuid4())
    )

    input_text = _build_agent_input(error_details, user_message, history)

    logger.info(
        "Bedrock agent invoked",
        extra={"agent_id": agent_id, "alias_id": agent_alias_id,
               "session_id": effective_session_id, "region": region},
    )

    try:
        last_exc = None
        for attempt in range(3):
            try:
                response = client.invoke_agent(
                    agentId=agent_id,
                    agentAliasId=agent_alias_id,
                    sessionId=effective_session_id,
                    inputText=input_text,
                )
                break
            except Exception as _exc:
                last_exc = _exc
                if attempt < 2:
                    time.sleep(2 ** attempt)  # 1 s, 2 s backoff
        else:
            raise last_exc

        reply_text = _extract_completion_text(response)
        if not reply_text:
            reply_text = "No response text returned by Bedrock agent."
        reply_text = _normalize_agent_reply(reply_text)

        return reply_text, {
            "model_id": f"agent:{agent_id}/{agent_alias_id}",
            "region": region,
            "session_id": effective_session_id,
        }
    except Exception as exc:
        raise RuntimeError(f"Bedrock agent request failed: {str(exc)}") from exc
