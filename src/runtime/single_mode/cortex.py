import asyncio
import logging
import os
from typing import Optional, Union

from actions.orchestrator import ActionOrchestrator
from backgrounds.orchestrator import BackgroundOrchestrator
from fuser import Fuser
from inputs.orchestrator import InputOrchestrator
from providers.io_provider import IOProvider
from providers.sleep_ticker_provider import SleepTickerProvider
from runtime.single_mode.config import RuntimeConfig, load_config
from simulators.orchestrator import SimulatorOrchestrator


class CortexRuntime:
    """
    The main entry point for the OM1 agent runtime environment.

    The CortexRuntime orchestrates communication between memory, fuser,
    actions, and manages inputs/outputs. It controls the agent's execution
    cycle and coordinates all major subsystems.

    Parameters
    ----------
    config : RuntimeConfig
        Configuration object containing all runtime settings including
        agent inputs, cortex LLM settings, and execution parameters.
    """

    config: RuntimeConfig
    fuser: Fuser
    action_orchestrator: ActionOrchestrator
    simulator_orchestrator: SimulatorOrchestrator
    background_orchestrator: BackgroundOrchestrator
    sleep_ticker_provider: SleepTickerProvider

    def __init__(
        self,
        config: RuntimeConfig,
        config_name: str,
        hot_reload: bool = False,
        check_interval: float = 60.0,
    ):
        """
        Initialize the CortexRuntime with provided configuration.

        Parameters
        ----------
        config : RuntimeConfig
            Configuration object for the runtime.
        config_name : str
            Name of the configuration file for hot-reload functionality.
        hot_reload : bool
            Whether to enable hot-reload functionality.
        check_interval : float
            Interval in seconds between config file checks for hot-reload.
        """
        self.config = config
        self.config_name = config_name
        self.hot_reload = hot_reload
        self.check_interval = check_interval

        self.fuser = Fuser(config)
        self.action_orchestrator = ActionOrchestrator(config)
        self.simulator_orchestrator = SimulatorOrchestrator(config)
        self.background_orchestrator = BackgroundOrchestrator(config)
        self.sleep_ticker_provider = SleepTickerProvider()
        self.io_provider = IOProvider()

        self.last_modified: float = 0.0
        self.config_watcher_task: Optional[asyncio.Task] = None
        self.input_listener_task: Optional[asyncio.Task] = None
        self.simulator_task: Optional[Union[asyncio.Task, asyncio.Future]] = None
        self.action_task: Optional[Union[asyncio.Task, asyncio.Future]] = None
        self.background_task: Optional[Union[asyncio.Task, asyncio.Future]] = None

        if self.hot_reload:
            self.config_path = os.path.join(
                os.path.dirname(__file__),
                "../../../config",
                self.config_name + ".json5",
            )
            self.last_modified = self._get_file_mtime()
            logging.info(
                f"Hot-reload enabled for config: {self.config_name} (check interval: {check_interval}s)"
            )

    async def run(self) -> None:
        """
        Start the runtime's main execution loop.

        This method initializes input listeners and begins the cortex
        processing loop, running them concurrently.

        Returns
        -------
        None
        """
        try:
            if self.hot_reload:
                self.config_watcher_task = asyncio.create_task(
                    self._check_config_changes()
                )

            await self._start_orchestrators()

            cortex_loop_task = asyncio.create_task(self._run_cortex_loop())

            while True:
                try:
                    awaitables = [cortex_loop_task]

                    if self.hot_reload and self.config_watcher_task:
                        awaitables.append(self.config_watcher_task)
                    if self.input_listener_task and not self.input_listener_task.done():
                        awaitables.append(self.input_listener_task)
                    if self.simulator_task and not self.simulator_task.done():
                        awaitables.append(self.simulator_task)  # type: ignore
                    if self.action_task and not self.action_task.done():
                        awaitables.append(self.action_task)  # type: ignore
                    if self.background_task and not self.background_task.done():
                        awaitables.append(self.background_task)  # type: ignore

                    await asyncio.gather(*awaitables)

                except asyncio.CancelledError:
                    logging.debug("Tasks cancelled during config reload, continuing...")
                    await asyncio.sleep(0.1)

                    if not cortex_loop_task.done():
                        continue
                    else:
                        break

                except Exception as e:
                    logging.error(f"Error in orchestrator tasks: {e}")
                    await asyncio.sleep(1.0)

        except Exception as e:
            logging.error(f"Error in cortex runtime: {e}")
            raise
        finally:
            await self._cleanup_tasks()

    def _get_file_mtime(self) -> float:
        """
        Get the modification time of the config file.

        Returns
        -------
        float
            Last modification time as a timestamp.
        """
        try:
            return os.path.getmtime(self.config_path)
        except OSError:
            return 0.0

    async def _check_config_changes(self) -> None:
        """
        Periodically check for config file changes and reload if needed.
        """
        while True:
            try:
                await asyncio.sleep(self.check_interval)

                current_mtime = self._get_file_mtime()
                if current_mtime > self.last_modified:
                    logging.info(f"Config file change detected: {self.config_name}")
                    await self._reload_config()
                    self.last_modified = current_mtime

            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error checking config changes: {e}")
                await asyncio.sleep(5)

    async def _reload_config(self) -> None:
        """
        Reload the configuration and restart all components.
        """
        try:
            logging.info(f"Reloading configuration: {self.config_name}")

            if self.config_name:
                new_config = load_config(self.config_name)
            else:
                logging.error("No config name available for reload")
                return

            await self._stop_current_orchestrators()

            self.config = new_config

            self.fuser = Fuser(new_config)
            self.action_orchestrator = ActionOrchestrator(new_config)
            self.simulator_orchestrator = SimulatorOrchestrator(new_config)
            self.background_orchestrator = BackgroundOrchestrator(new_config)

            await self._start_orchestrators()

            logging.info("Configuration reloaded successfully")

        except Exception as e:
            logging.error(f"Failed to reload configuration: {e}")
            logging.error("Continuing with previous configuration")

    async def _stop_current_orchestrators(self) -> None:
        """
        Stop all current orchestrator tasks gracefully.
        """
        logging.debug("Stopping current orchestrators for reload...")

        tasks_to_cancel = []

        if self.input_listener_task and not self.input_listener_task.done():
            logging.debug("Cancelling input listener task")
            tasks_to_cancel.append(self.input_listener_task)

        if self.simulator_task and not self.simulator_task.done():
            logging.debug("Cancelling simulator task")
            tasks_to_cancel.append(self.simulator_task)

        if self.action_task and not self.action_task.done():
            logging.debug("Cancelling action task")
            tasks_to_cancel.append(self.action_task)

        if self.background_task and not self.background_task.done():
            logging.debug("Cancelling background task")
            tasks_to_cancel.append(self.background_task)

        for task in tasks_to_cancel:
            task.cancel()

        if tasks_to_cancel:
            try:
                await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
                logging.debug(
                    f"Successfully cancelled {len(tasks_to_cancel)} orchestrator tasks"
                )
            except Exception as e:
                logging.warning(f"Error during orchestrator shutdown: {e}")

        self.input_listener_task = None
        self.simulator_task = None
        self.action_task = None
        self.background_task = None

        logging.debug("Orchestrators stopped successfully")

    async def _start_orchestrators(self) -> None:
        """
        Start orchestrators for the current config.
        """
        logging.debug("Starting orchestrators...")

        input_orchestrator = InputOrchestrator(self.config.agent_inputs)
        self.input_listener_task = asyncio.create_task(input_orchestrator.listen())

        if self.simulator_orchestrator:
            self.simulator_task = self.simulator_orchestrator.start()
        if self.action_orchestrator:
            self.action_task = self.action_orchestrator.start()
        if self.background_orchestrator:
            self.background_task = self.background_orchestrator.start()

        logging.debug("Orchestrators started successfully")

    async def _cleanup_tasks(self) -> None:
        """
        Cleanup all running tasks gracefully.
        """
        tasks_to_cancel = []

        if (
            self.hot_reload
            and self.config_watcher_task
            and not self.config_watcher_task.done()
        ):
            tasks_to_cancel.append(self.config_watcher_task)
        if self.input_listener_task and not self.input_listener_task.done():
            tasks_to_cancel.append(self.input_listener_task)
        if self.simulator_task and not self.simulator_task.done():
            tasks_to_cancel.append(self.simulator_task)
        if self.action_task and not self.action_task.done():
            tasks_to_cancel.append(self.action_task)
        if self.background_task and not self.background_task.done():
            tasks_to_cancel.append(self.background_task)

        for task in tasks_to_cancel:
            task.cancel()

        if tasks_to_cancel:
            try:
                await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
            except Exception as e:
                logging.warning(f"Error during final cleanup: {e}")

        logging.debug("Tasks cleaned up successfully")

    async def _start_input_listeners(self) -> asyncio.Task:
        """
        Initialize and start input listeners.

        Creates an InputOrchestrator for the configured agent inputs
        and starts listening for input events.

        Returns
        -------
        asyncio.Task
            Task handling input listening operations.
        """
        input_orchestrator = InputOrchestrator(self.config.agent_inputs)
        input_listener_task = asyncio.create_task(input_orchestrator.listen())
        return input_listener_task

    async def _start_simulator_task(self) -> asyncio.Future:
        return self.simulator_orchestrator.start()

    async def _start_action_task(self) -> asyncio.Future:
        return self.action_orchestrator.start()

    async def _start_background_task(self) -> asyncio.Future:
        return self.background_orchestrator.start()

    async def _run_cortex_loop(self) -> None:
        """
        Execute the main cortex processing loop.

        Runs continuously, managing the sleep/wake cycle and triggering
        tick operations at the configured frequency.

        Returns
        -------
        None
        """
        while True:
            if not self.sleep_ticker_provider.skip_sleep:
                await self.sleep_ticker_provider.sleep(1 / self.config.hertz)
            await self._tick()
            self.sleep_ticker_provider.skip_sleep = False

    async def _tick(self) -> None:
        """
        Execute a single tick of the cortex processing cycle.

        Collects inputs, generates prompts, processes them through the LLM,
        and triggers appropriate simulators and actions based on the output.

        Returns
        -------
        None
        """
        # collect all the latest inputs
        finished_promises, _ = await self.action_orchestrator.flush_promises()

        # combine those inputs into a suitable prompt
        prompt = self.fuser.fuse(self.config.agent_inputs, finished_promises)
        if prompt is None:
            logging.warning("No prompt to fuse")
            return

        # if there is a prompt, send to the AIs
        output = await self.config.cortex_llm.ask(prompt)
        if output is None:
            logging.warning("No output from LLM")
            return

        # Trigger the simulators
        await self.simulator_orchestrator.promise(output.actions)

        # Trigger the actions
        await self.action_orchestrator.promise(output.actions)
