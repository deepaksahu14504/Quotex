import logging
import time

from pyquotex.expiration import get_expiration_time_quotex
from pyquotex.utils import json_utils as json
from pyquotex.ws.channels.base import Base

logger = logging.getLogger(__name__)


class Buy(Base):
    """Class for Quotex buy websocket channel."""

    name = "buy"

    async def __call__(
            self,
            price: float | int,
            asset: str,
            direction: str,
            duration: int,
            request_id: int,
            is_fast_option: bool,
            time_mode: str,
    ) -> None:
        option_type = 3 if is_fast_option else 1

        expiration_time = get_expiration_time_quotex(
            int(time.time()),
            duration
        )
        expiration = expiration_time

        """if asset.endswith("_otc") and not is_fast_option:
            option_type = 100
            expiration = duration"""

        # Modo TIMER: funciona para OTC e não-OTC
        if time_mode == "TIMER" and not is_fast_option:
            option_type = 100
            expiration = duration

        # BUG FIX (2026-08): the `time` field means different things per
        # optionType, and the fast-option branch was sending the wrong one.
        #
        #   optionType 1   -> `time` is an ABSOLUTE expiry timestamp
        #   optionType 3   -> `time` is a DURATION in seconds
        #   optionType 100 -> `time` is a DURATION in seconds (handled above)
        #
        # `expiration` above is always the absolute timestamp from
        # get_expiration_time_quotex(). For optionType 3 that put an epoch
        # value like 1787426796 into a field the broker reads as a number of
        # seconds. Quotex does not reply with an error to a payload it cannot
        # parse -- it simply never answers, so buy() sat waiting for a
        # confirmation that was never coming and every order ended in
        # "Timeout waiting for buy confirmation" while the frame itself had
        # gone out cleanly. That is exactly what the websocket trace showed:
        # `> 42["orders/open",...,"optionType":3]` sent, and no `orders/open`
        # response of any kind before the wait expired.
        if option_type == 3:
            expiration = duration

        if option_type == 1 and duration < 60:
            print(
                f"{duration}s duration is not allowed for this type of "
                "operation, except for OTC assets. 60 seconds will be added "
                "to meet Quotex requirements."
            )

        await self.api.settings_apply(
            asset,
            expiration,
            is_fast_option=is_fast_option,
            end_time=expiration_time,
        )

        payload = {
            "asset": asset,
            "amount": price,
            "time": expiration,
            "action": direction,
            "isDemo": self.api.account_type,
            "tournamentId": self.api.tournament_id,
            "requestId": request_id,
            "optionType": option_type
        }

        data = '42["tick"]'
        await self.send_websocket_request(data)

        data = f'42["orders/open",{json.dumps_str(payload)}]'
        logger.debug(data)
        await self.send_websocket_request(data)
