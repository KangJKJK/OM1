import logging
import threading
from typing import Optional, Set

import requests

from .io_provider import IOProvider
from .singleton import singleton


@singleton
class RoomTypeLocationProvider:
    """
    Provider that reads the `room_type` dynamic variable from IOProvider in a
    background thread and POSTs it to the map locations API.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:5000/maps/locations/add/slam",
        timeout: int = 5,
        refresh_interval: int = 5,
        map_name: str = "map",
    ):
        """
        Initialize the provider.

        Parameters
        ----------
        base_url : str
            The HTTP endpoint to POST room-based locations to.
            Default is "http://localhost:5000/maps/locations/add/slam".
        timeout : int
            Timeout for HTTP POST requests in seconds.
        refresh_interval : int
            How often to check the IOProvider for a room_type in seconds.
        map_name : str
            The map name to include in the payload.
        """
        self.base_url = base_url
        self.timeout = timeout
        self.refresh_interval = refresh_interval
        self.map_name = map_name

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._posted_room_types: Set[str] = set()

        self.io_provider = IOProvider()

        # Allowed room type set (must match the values used elsewhere)
        self._allowed_room_types = {
            "living_room",
            "bedroom",
            "study",
            "kitchen",
            "outdoor",
        }

    def start(self) -> None:
        """
        Start the background thread that monitors room_type and posts it.
        """
        if self._thread and self._thread.is_alive():
            logging.warning("RoomTypeLocationProvider already running")
            return

        if not self.base_url:
            logging.error(
                "RoomTypeLocationProvider missing 'base_url'; "
                "provider will not start."
            )
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logging.info(
            "RoomTypeLocationProvider background thread started "
            f"(base_url: {self.base_url}, refresh: {self.refresh_interval}s)"
        )

    def stop(self) -> None:
        """
        Stop the background thread.
        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            logging.info("RoomTypeLocationProvider background thread stopped")

    def _run(self) -> None:
        """
        Background loop: periodically check IOProvider for room_type and POST.
        """
        while not self._stop_event.is_set():
            try:
                self._process_room_type()
            except Exception:
                logging.exception("Error in RoomTypeLocationProvider loop")

            # Wait until next cycle or until stop event is set
            self._stop_event.wait(timeout=self.refresh_interval)

    def _get_current_room_type(self) -> Optional[str]:
        """
        Read the current room_type from IOProvider.

        Returns
        -------
        Optional[str]
            The current normalized room type, or None if not set/invalid.
        """
        try:
            room_type = self.io_provider.get_dynamic_variable("room_type")
        except Exception:
            logging.exception("Failed to read 'room_type' from IOProvider")
            return None

        if not room_type:
            return None

        # Ensure it's a string and normalized
        if not isinstance(room_type, str):
            room_type = str(room_type)

        room_type = room_type.strip().lower()

        if room_type not in self._allowed_room_types:
            logging.debug(
                f"Room type '{room_type}' found in dynamic variables, "
                "but not in allowed set; ignoring."
            )
            return None

        return room_type

    def _process_room_type(self) -> None:
        """
        Check the current room type and POST it.
        """
        if not self.base_url:
            # Already logged in start(), just silently return here
            return

        room_type = self._get_current_room_type()
        if not room_type:
            # No valid room type available
            return

        payload = {
            "map_name": self.map_name,
            "label": room_type,
            "description": f"Auto-generated location for room type '{room_type}'",
        }

        try:
            logging.info(
                f"Posting room_type '{room_type}' to {self.base_url} with payload: {payload}"
            )
            resp = requests.post(self.base_url, json=payload, timeout=self.timeout)
            text = resp.text

            if 200 <= resp.status_code < 300:
                logging.info(
                    f"Room type location stored '{room_type}' -> "
                    f"{resp.status_code} {text}"
                )
                with self._lock:
                    self._posted_room_types.add(room_type)
            else:
                logging.error(
                    f"Room type location API returned {resp.status_code}: {text}"
                )

        except requests.Timeout:
            logging.error("Room type location API request timed out")
        except Exception as e:
            logging.error(f"Room type location API request failed: {e}")
