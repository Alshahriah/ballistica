#!/usr/bin/env python3.8
# Released under the MIT License. See LICENSE for details.
#
"""BallisticaCore server manager."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Lock, Thread, current_thread
from typing import TYPE_CHECKING

# We make use of the bacommon and efro packages as well as site-packages
# included with our bundled Ballistica dist, so we need to add those paths
# before we import them.
sys.path += [
    str(Path(Path(__file__).parent, 'dist', 'ba_data', 'python')),
    str(Path(Path(__file__).parent, 'dist', 'ba_data', 'python-site-packages'))
]

from bacommon.servermanager import ServerConfig, StartServerModeCommand
from efro.dataclasses import dataclass_assign, dataclass_validate
from efro.error import CleanError
from efro.terminal import Clr

if TYPE_CHECKING:
    from typing import Optional, List, Dict, Union, Tuple
    from types import FrameType
    from bacommon.servermanager import ServerCommand

# Not sure how much versioning we'll do with this, but this will get
# printed at startup in case we need it.
VERSION_STR = '1.1.0'


class ServerManagerApp:
    """An app which manages BallisticaCore server execution.

    Handles configuring, launching, re-launching, and otherwise
    managing BallisticaCore operating in server mode.
    """

    def __init__(self) -> None:
        try:
            self._config = self._load_config()
        except Exception as exc:
            raise CleanError(f'Error loading config: {exc}') from exc
        self._shutdown_desired = False
        self._done = False
        self._subprocess_commands: List[Union[str, ServerCommand]] = []
        self._subprocess_commands_lock = Lock()
        self._restart_minutes: Optional[float] = None
        self._running_interactive = False
        self._subprocess: Optional[subprocess.Popen[bytes]] = None
        self._launch_time = time.time()
        self._subprocess_launch_time: Optional[float] = None
        self._subprocess_sent_auto_restart = False
        self._subprocess_sent_unclean_exit = False
        self._subprocess_thread: Optional[Thread] = None

        # If we don't have any explicit exit conditions set,
        # we run indefinitely (though we restart our subprocess
        # periodically to clear out leaks/cruft)
        if (self._config.clean_exit_minutes is None
                and self._config.unclean_exit_minutes is None
                and self._config.idle_exit_minutes is None):
            self._restart_minutes = 360.0

    @property
    def config(self) -> ServerConfig:
        """The current config for the app."""
        return self._config

    @config.setter
    def config(self, value: ServerConfig) -> None:
        dataclass_validate(value)
        self._config = value

    @property
    def restart_minutes(self) -> Optional[float]:
        """The time between automatic server restarts.

        Restarting the server periodically can minimize the effect of
        memory leaks or other built-up cruft.
        """
        return self._restart_minutes

    def run_interactive(self) -> None:
        """Run the app loop to completion."""
        import code

        if self._running_interactive:
            raise RuntimeError('Already running interactively.')
        self._running_interactive = True

        # Print basic usage info in interactive mode.
        if sys.stdin.isatty():
            if __debug__:
                modestr = '(debug mode)'
            else:
                modestr = '(opt mode)'
            print(f'{Clr.CYN}{Clr.BLD}BallisticaCore server'
                  f' manager {VERSION_STR}'
                  f' starting up {modestr}...{Clr.RST}\n'
                  f'{Clr.CYN}Use the "mgr" object to make'
                  f' live server adjustments.\n'
                  f'Type "help(mgr)" for more information.{Clr.RST}')

        # Python will handle SIGINT for us (as KeyboardInterrupt) but we
        # need to register a SIGTERM handler so we have a chance to clean
        # up our child-process when someone tells us to die. (and avoid
        # zombie processes)
        signal.signal(signal.SIGTERM, self._handle_term_signal)

        # Fire off a background thread to wrangle our server binaries.
        self._subprocess_thread = Thread(target=self._bg_thread_main)
        self._subprocess_thread.start()

        context = {'__name__': '__console__', '__doc__': None, 'mgr': self}

        # Enable tab-completion if possible.
        self._enable_tab_completion(context)

        # Now just sit in an interpreter.
        # TODO: make it possible to use IPython if the user has it available.
        try:
            code.interact(local=context, banner='', exitmsg='')
        except SystemExit:
            # We get this from the builtin quit(), etc.
            # Need to catch this so we can clean up, otherwise we'll be
            # left in limbo with our process thread still running.
            pass
        except BaseException as exc:
            # Installing Python 3.7 on Ubuntu 18 can lead to this error;
            # inform the user how to fix it.
            if "No module named 'apt_pkg'" in str(exc):
                print(f'{Clr.SRED}Error: Your Python environment needs to'
                      ' be fixed (apt_pkg cannot be found).\n'
                      f'See the final step in the Linux instructions here:\n'
                      f'  https://github.com/efroemling/ballistica/'
                      f'wiki/Getting-Started#linux{Clr.RST}')
            else:
                print(f'{Clr.SRED}Unexpected interpreter exception:'
                      f' {exc} ({type(exc)}){Clr.RST}')

        # Mark ourselves as shutting down and wait for the process to wrap up.
        self._done = True
        self._subprocess_thread.join()

    def cmd(self, statement: str) -> None:
        """Exec a Python command on the current running server child-process.

        Note that commands are executed asynchronously and no status or
        return value is accessible from this manager app.
        """
        if not isinstance(statement, str):
            raise TypeError(f'Expected a string arg; got {type(statement)}')
        with self._subprocess_commands_lock:
            self._subprocess_commands.append(statement)
        self._block_for_command_completion()

    def _block_for_command_completion(self) -> None:
        # Ideally we'd block here until the command was run so our prompt would
        # print after it's results. We currently don't get any response from
        # the app so the best we can do is block until our bg thread has sent
        # it. In the future we can perhaps add a proper 'command port'
        # interface for proper blocking two way communication.
        while True:
            with self._subprocess_commands_lock:
                if not self._subprocess_commands:
                    break
            time.sleep(0.1)

        # One last short delay so if we come out *just* as the command is sent
        # we'll hopefully still give it enough time to process/print.
        time.sleep(0.1)

    def screenmessage(self,
                      message: str,
                      color: Optional[Tuple[float, float, float]] = None,
                      clients: Optional[List[int]] = None) -> None:
        """Display a screen-message.

        This will have no name attached and not show up in chat history.
        They will show up in replays, however (unless clients is passed).
        """
        from bacommon.servermanager import ScreenMessageCommand
        self._enqueue_server_command(
            ScreenMessageCommand(message=message, color=color,
                                 clients=clients))

    def chatmessage(self,
                    message: str,
                    clients: Optional[List[int]] = None) -> None:
        """Send a chat message from the server.

        This will have the server's name attached and will be logged
        in client chat windows, just like other chat messages.
        """
        from bacommon.servermanager import ChatMessageCommand
        self._enqueue_server_command(
            ChatMessageCommand(message=message, clients=clients))

    def clientlist(self) -> None:
        """Print a list of connected clients."""
        from bacommon.servermanager import ClientListCommand
        self._enqueue_server_command(ClientListCommand())
        self._block_for_command_completion()

    def kick(self, client_id: int, ban_time: Optional[int] = None) -> None:
        """Kick the client with the provided id.

        If ban_time is provided, the client will be banned for that
        length of time in seconds. If it is None, ban duration will
        be determined automatically. Pass 0 or a negative number for no
        ban time.
        """
        from bacommon.servermanager import KickCommand
        self._enqueue_server_command(
            KickCommand(client_id=client_id, ban_time=ban_time))

    def restart(self, immediate: bool = False) -> None:
        """Restart the server child-process.

        This can be necessary for some config changes to take effect.
        By default, the server will restart at the next good transition
        point (end of a series, etc) but passing immediate=True will restart
        it immediately.
        """
        from bacommon.servermanager import ShutdownCommand, ShutdownReason
        self._enqueue_server_command(
            ShutdownCommand(reason=ShutdownReason.RESTARTING,
                            immediate=immediate))

    def shutdown(self, immediate: bool = False) -> None:
        """Shut down the server child-process and exit the wrapper

        By default, the server will exit at the next good transition
        point (end of a series, etc) but passing immediate=True will stop
        it immediately.
        """
        from bacommon.servermanager import ShutdownCommand, ShutdownReason
        self._enqueue_server_command(
            ShutdownCommand(reason=ShutdownReason.NONE, immediate=immediate))

        # So we know to bail completely once this subprocess completes.
        self._shutdown_desired = True

    def _load_config(self) -> ServerConfig:
        user_config_path = 'config.yaml'

        # Start with a default config, and if there is a config.yaml,
        # assign whatever is contained within.
        config = ServerConfig()
        if os.path.exists(user_config_path):
            import yaml
            with open(user_config_path) as infile:
                user_config = yaml.safe_load(infile.read())

            # An empty config file will yield None, and that's ok.
            if user_config is not None:
                dataclass_assign(config, user_config)

        return config

    def _enable_tab_completion(self, locs: Dict) -> None:
        """Enable tab-completion on platforms where available (linux/mac)."""
        try:
            import readline
            import rlcompleter
            readline.set_completer(rlcompleter.Completer(locs).complete)
            readline.parse_and_bind('tab:complete')
        except ImportError:
            # This is expected (readline doesn't exist under windows).
            pass

    def _bg_thread_main(self) -> None:
        """Top level method run by our bg thread."""
        while not self._done:
            self._run_server_cycle()

    def _handle_term_signal(self, sig: int, frame: FrameType) -> None:
        """Handle signals (will always run in the main thread)."""
        del sig, frame  # Unused.
        raise SystemExit()

    def _run_server_cycle(self) -> None:
        """Spin up the server child-process and run it until exit."""

        self._prep_subprocess_environment()

        # Launch the binary and grab its stdin;
        # we'll use this to feed it commands.
        self._subprocess_launch_time = time.time()

        # Set an environment var so the server process knows its being
        # run under us. This causes it to ignore ctrl-c presses and other
        # slight behavior tweaks. Hmm; should this be an argument instead?
        os.environ['BA_SERVER_WRAPPER_MANAGED'] = '1'

        print(f'{Clr.CYN}Launching server child-process...{Clr.RST}')
        binary_name = ('ballisticacore_headless.exe'
                       if os.name == 'nt' else './ballisticacore_headless')
        self._subprocess = subprocess.Popen(
            [binary_name, '-cfgdir', 'ba_root'],
            stdin=subprocess.PIPE,
            cwd='dist')

        # Do the thing.
        # No matter how this ends up, make sure the process is dead after.
        try:
            self._run_subprocess_until_exit()
        finally:
            self._kill_subprocess()

        if self._shutdown_desired:
            self._done = True

            # Our main thread will still be blocked in its prompt or whatnot;
            # let it know it should die.
            os.kill(os.getpid(), signal.SIGTERM)

    def _prep_subprocess_environment(self) -> None:
        """Write files that must exist at process launch."""
        os.makedirs('dist/ba_root', exist_ok=True)
        if os.path.exists('dist/ba_root/config.json'):
            with open('dist/ba_root/config.json') as infile:
                bincfg = json.loads(infile.read())
        else:
            bincfg = {}

        # Some of our config values translate directly into the
        # ballisticacore config file; the rest we pass at runtime.
        bincfg['Port'] = self._config.port
        bincfg['Auto Balance Teams'] = self._config.auto_balance_teams
        bincfg['Show Tutorial'] = False
        with open('dist/ba_root/config.json', 'w') as outfile:
            outfile.write(json.dumps(bincfg))

    def _enqueue_server_command(self, command: ServerCommand) -> None:
        """Enqueue a command to be sent to the server.

        Can be called from any thread.
        """
        with self._subprocess_commands_lock:
            self._subprocess_commands.append(command)

    def _send_server_command(self, command: ServerCommand) -> None:
        """Send a command to the server.

        Must be called from the server process thread.
        """
        import pickle
        assert current_thread() is self._subprocess_thread
        assert self._subprocess is not None
        assert self._subprocess.stdin is not None
        val = repr(pickle.dumps(command))
        assert '\n' not in val
        execcode = (f'import ba._servermode;'
                    f' ba._servermode._cmd({val})\n').encode()
        self._subprocess.stdin.write(execcode)
        self._subprocess.stdin.flush()

    def _run_subprocess_until_exit(self) -> None:
        assert self._subprocess is not None
        assert self._subprocess.stdin is not None
        assert self._subprocess_launch_time is not None

        # Send the initial server config which should kick things off.
        # (but make sure its values are still valid first)
        dataclass_validate(self._config)
        self._send_server_command(StartServerModeCommand(self._config))

        while True:

            # If the app is trying to shut down, nope out immediately.
            if self._done:
                break

            # Pass along any commands to our process.
            with self._subprocess_commands_lock:
                for incmd in self._subprocess_commands:
                    # If we're passing a raw string to exec, no need to wrap it
                    # in any proper structure.
                    if isinstance(incmd, str):
                        self._subprocess.stdin.write((incmd + '\n').encode())
                        self._subprocess.stdin.flush()
                    else:
                        self._send_server_command(incmd)
                self._subprocess_commands = []

            # Request restarts/shut-downs for various reasons.
            sincelaunch = time.time() - self._subprocess_launch_time
            if (self._restart_minutes is not None and sincelaunch >
                (self._restart_minutes * 60.0)
                    and not self._subprocess_sent_auto_restart):
                print(f'{Clr.CYN}restart_minutes ({self._restart_minutes})'
                      f' elapsed; requesting child-process'
                      f' soft restart...{Clr.RST}')
                self.restart()
                self._subprocess_sent_auto_restart = True
            if self._config.unclean_exit_minutes is not None:
                elapsed = (time.time() - self._launch_time) / 60.0
                threshold = self._config.unclean_exit_minutes
                if (elapsed > threshold
                        and not self._subprocess_sent_unclean_exit):
                    print(f'{Clr.CYN}unclean_exit_minutes'
                          f' ({threshold})'
                          f' elapsed; requesting child-process'
                          f' shutdown...{Clr.RST}')
                    self.shutdown(immediate=True)
                    self._subprocess_sent_unclean_exit = True

            # Watch for the process exiting..
            code: Optional[int] = self._subprocess.poll()
            if code is not None:
                if code == 0:
                    clr = Clr.CYN
                    slp = 0.0
                else:
                    clr = Clr.SRED
                    slp = 5.0  # Avoid super fast death loops.
                print(f'{clr}Server child-process exited'
                      f' with code {code}.{Clr.RST}')
                self._reset_subprocess_vars()
                time.sleep(slp)
                break

            time.sleep(0.25)

    def _reset_subprocess_vars(self) -> None:
        self._subprocess = None
        self._subprocess_launch_time = None
        self._subprocess_sent_auto_restart = False
        self._subprocess_sent_unclean_exit = False

    def _kill_subprocess(self) -> None:
        """End the server process if it still exists."""
        assert current_thread() is self._subprocess_thread
        if self._subprocess is None:
            return

        print(f'{Clr.CYN}Stopping server process...{Clr.RST}')

        # First, ask it nicely to die and give it a moment.
        # If that doesn't work, bring down the hammer.
        self._subprocess.terminate()
        try:
            self._subprocess.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._subprocess.kill()
        self._reset_subprocess_vars()
        print(f'{Clr.CYN}Server process stopped.{Clr.RST}')


def main() -> None:
    """Run a BallisticaCore server manager in interactive mode."""
    try:
        # ServerManager expects cwd to be the server dir (containing
        # dist/, config.yaml, etc.)
        # Let's change our working directory to the location of this file
        # so we can run this script from anywhere and it'll work.
        os.chdir(os.path.abspath(os.path.dirname(__file__)))

        ServerManagerApp().run_interactive()
    except CleanError as exc:
        # For clean errors, do a simple print and fail; no tracebacks/etc.
        exc.pretty_print()
        sys.exit(1)


if __name__ == '__main__':
    main()
