import re
import time
from collections.abc import Callable
from collections.abc import Generator
from functools import wraps
from typing import Any
from typing import cast

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.web import SlackResponse

from danswer.utils.logger import setup_logger

logger = setup_logger()

# number of messages we request per page when fetching paginated slack messages
_SLACK_LIMIT = 900


def get_message_link(
    event: dict[str, Any], workspace: str, channel_id: str | None = None
) -> str:
    channel_id = channel_id or cast(
        str, event["channel"]
    )  # channel must either be present in the event or passed in
    message_ts = cast(str, event["ts"])
    message_ts_without_dot = message_ts.replace(".", "")
    return (
        f"https://{workspace}.slack.com/archives/{channel_id}/p{message_ts_without_dot}"
    )


def make_slack_api_call_logged(
    call: Callable[..., SlackResponse],
) -> Callable[..., SlackResponse]:
    @wraps(call)
    def logged_call(**kwargs: Any) -> SlackResponse:
        logger.debug(f"Making call to Slack API '{call.__name__}' with args '{kwargs}'")
        result = call(**kwargs)
        logger.debug(f"Call to Slack API '{call.__name__}' returned '{result}'")
        return result

    return logged_call


def make_slack_api_call_paginated(
    call: Callable[..., SlackResponse],
) -> Callable[..., Generator[dict[str, Any], None, None]]:
    """Wraps calls to slack API so that they automatically handle pagination"""

    @wraps(call)
    def paginated_call(**kwargs: Any) -> Generator[dict[str, Any], None, None]:
        cursor: str | None = None
        has_more = True
        while has_more:
            response = call(cursor=cursor, limit=_SLACK_LIMIT, **kwargs)
            yield cast(dict[str, Any], response.validate())
            cursor = cast(dict[str, Any], response.get("response_metadata", {})).get(
                "next_cursor", ""
            )
            has_more = bool(cursor)

    return paginated_call


def make_slack_api_rate_limited(
    call: Callable[..., SlackResponse], max_retries: int = 3
) -> Callable[..., SlackResponse]:
    """Wraps calls to slack API so that they automatically handle rate limiting"""

    @wraps(call)
    def rate_limited_call(**kwargs: Any) -> SlackResponse:
        for _ in range(max_retries):
            try:
                # Make the API call
                response = call(**kwargs)

                # Check for errors in the response, will raise `SlackApiError`
                # if anything went wrong
                response.validate()
                return response

            except SlackApiError as e:
                if e.response["error"] == "ratelimited":
                    # Handle rate limiting: get the 'Retry-After' header value and sleep for that duration
                    retry_after = int(e.response.headers.get("Retry-After", 1))
                    logger.info(
                        f"Slack call rate limited, retrying after {retry_after} seconds. Exception: {e}"
                    )
                    time.sleep(retry_after)
                else:
                    # Raise the error for non-transient errors
                    raise

        # If the code reaches this point, all retries have been exhausted
        raise Exception(f"Max retries ({max_retries}) exceeded")

    return rate_limited_call


class UserIdReplacer:
    """Utility class to replace user IDs with usernames in a message.
    Handles caching, so the same request is not made multiple times
    for the same user ID"""

    def __init__(self, client: WebClient) -> None:
        self._client = client
        self._user_id_to_name_map: dict[str, str] = {}

    def _get_slack_user_name(self, user_id: str) -> str:
        if user_id not in self._user_id_to_name_map:
            try:
                response = make_slack_api_rate_limited(self._client.users_info)(
                    user=user_id
                )
                # prefer display name if set, since that is what is shown in Slack
                self._user_id_to_name_map[user_id] = (
                    response["user"]["profile"]["display_name"]
                    or response["user"]["profile"]["real_name"]
                )
            except SlackApiError as e:
                logger.exception(
                    f"Error fetching data for user {user_id}: {e.response['error']}"
                )
                raise

        return self._user_id_to_name_map[user_id]

    def replace_user_ids_with_names(self, message: str) -> str:
        # Find user IDs in the message
        user_ids = re.findall("<@(.*?)>", message)

        # Iterate over each user ID found
        for user_id in user_ids:
            try:
                if user_id in self._user_id_to_name_map:
                    user_name = self._user_id_to_name_map[user_id]
                else:
                    user_name = self._get_slack_user_name(user_id)

                # Replace the user ID with the username in the message
                message = message.replace(f"<@{user_id}>", f"@{user_name}")
            except Exception:
                logger.exception(
                    f"Unable to replace user ID with username for user_id '{user_id}"
                )

        return message
