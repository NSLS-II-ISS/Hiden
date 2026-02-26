from pathlib import PureWindowsPath
import socket
import time
import logging
import threading
import os

# Logger setup (avoid duplicate handlers when module imported repeatedly)
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("massoft_client.log")]
    )

# System Configuration
MAS_HOST = '10.66.58.225'
MAS_PORT = 5026
EXPERIMENT_DIRECTORY = r"C:\Users\08id-user\Documents\Hiden Analytical\MASsoft\11"
<<<<<<< HEAD
EXPERIMENT_DIRECTORY_ENV = "HIDEN_FilePath"
TEMPLATE_DICT = {
    "exp1": "HIDEN_1.exp", "exp2": "HIDEN_2.exp", "exp3": "HIDEN_3.exp", "exp4": "HIDEN_4.exp",
}
MOST_RECENT_FILE = "HIDEN_LastFile"


class MASsoftSocket:
    def __init__(self, host, port, name="GenericSocket", timeout=5, max_retries=2, retry_delay=0.5):
=======
EXPERIMENT_DIRECTORY_ENV = "%HIDEN_FilePath%" # Environment variable name for the experiment directory
MOST_RECENT_FILE = "%HIDEN_LastFile%" # This environment variable name already includes the path
TIME_PERSISTANCE = 20 # Time in seconds for the messages to keep trying waiting for success
MESSAGE_TERMINATOR = "\r\n"

class MASsoftSocket:
    def __init__(self, host, port, name="GenericSocket", timeout=20):
>>>>>>> 0c250acc164651cb67d67172c79817375ce6ba28
        self.host = host
        self.port = port
        self.name = name
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.sock = None
        self.lock = threading.Lock()

    def connect(self):
        """Establish or re-establish the socket connection (idempotent)."""
        with self.lock:
            if self.sock:
                try:
                    self.sock.sendall(b'')
                    return
                except Exception:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None

            last_exc = None
            for attempt in range(1, self.max_retries + 2):
                try:
                    self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                    self.sock.settimeout(self.timeout)
                    logging.info(f"{self.name} connected to {self.host}:{self.port}")
                    try:
                        _ = self.sock.recv(4096)
                    except socket.timeout:
                        pass
                    return
                except Exception as ex:
                    last_exc = ex
                    logging.warning(f"{self.name} connect attempt {attempt} failed: {ex}")
                    time.sleep(self.retry_delay * attempt)
            raise ConnectionError(f"{self.name} failed to connect: {last_exc}")

    def send_command(self, command, expect_response=True):
<<<<<<< HEAD
        """Send a command, reconnecting on transient errors. Returns response string or ''."""
        message = command.strip() + ' -d20\r\n'
=======
        if not self.sock:
            raise RuntimeError(f"{self.name} not connected.")
        # Append retry delay and CRLF
        message = command.strip() + f' -d{TIME_PERSISTANCE}{MESSAGE_TERMINATOR}'
        self.sock.sendall(message.encode('utf-8'))
        if expect_response:
            try:
                resp = self.sock.recv(4096).decode('utf-8').strip()
            except socket.timeout:
                logging.warning(f"{self.name} response timeout for: {message.strip()}")
                return ''
            logging.info(f"{self.name} | {message.strip()} => {resp}")
            return resp
        return ''
>>>>>>> 0c250acc164651cb67d67172c79817375ce6ba28

        with self.lock:
            for attempt in range(1, self.max_retries + 2):
                try:
                    if not self.sock:
                        self.connect()
                    self.sock.sendall(message.encode('utf-8'))
                    if expect_response:
                        try:
                            resp = self.sock.recv(8192).decode('utf-8').strip()
                        except socket.timeout:
                            logging.warning(f"{self.name} response timeout for: {message.strip()}")
                            return ''
                        logging.info(f"{self.name} | {message.strip()} => {resp}")
                        return resp
                    return ''
                except Exception as ex:
                    logging.warning(f"{self.name} send attempt {attempt} failed: {ex}")
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
                    time.sleep(self.retry_delay * attempt)
            logging.error(f"{self.name} failed to send command after retries: {message.strip()}")
            return ''

    def receive(self):
        """Receive raw data (non-blocking with socket timeout)."""
        with self.lock:
            if not self.sock:
                try:
                    self.connect()
                except Exception as ex:
                    logging.warning(f"{self.name} receive failed to connect: {ex}")
                    return ''
            try:
                return self.sock.recv(8192).decode('utf-8').strip()
            except socket.timeout:
                return ''
            except Exception as ex:
                logging.warning(f"{self.name} receive error: {ex}")
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
                return ''

    def close(self):
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
                logging.info(f"{self.name} closed.")


class MASsoftClient:
    def __init__(self, host=MAS_HOST, port=MAS_PORT):
        self.command_socket = MASsoftSocket(host, port, name="CommandSocket")
        self.status_socket = MASsoftSocket(host, port, name="StatusSocket")
        self.data_socket = MASsoftSocket(host, port, name="DataSocket")
        self.current_file = None

    def initialize(self):
        """Connect all sockets (retries handled by sockets)."""
        self.command_socket.connect()
        self.status_socket.connect()
        self.data_socket.connect()

<<<<<<< HEAD
    def open_experiment(self, file_name=None):
        """Open and associate an experiment file.

        If `file_name` is None, query MASsoft for the currently associated file.
        """
        if file_name is None:
            full_path = self.query_filename()
        else:
            base_dir = os.environ.get(EXPERIMENT_DIRECTORY_ENV, EXPERIMENT_DIRECTORY)
            full_path = str(PureWindowsPath(base_dir) / file_name)
            if not os.path.isfile(full_path):
                raise FileNotFoundError(f"File not found: {full_path}")

=======
    def open_experiment_commands(self, file_name=None):
        """Open and associate an experiment file.
        If file_name is None, query MASsoft for the current filename."""
        # 1) Determine the filename string
        if file_name is None:
            full_path = self.query_filename()
        else:
            # Coerce list or tuple into a single string
            if isinstance(file_name, (list, tuple)):
                file_name = file_name[0]
            # Build a pure-Windows path
            full_path = str(PureWindowsPath(EXPERIMENT_DIRECTORY) / file_name)

        # 2) Send to MASsoft
>>>>>>> 0c250acc164651cb67d67172c79817375ce6ba28
        resp = self.command_socket.send_command(f'-f"{full_path}"')
        if resp =='0':
            raise RuntimeError(f"Failed to open experiment file: {full_path}")

        # 3) Remember it for future operations
        self.current_file = full_path
        return full_path

<<<<<<< HEAD
    def run_experiment(self, mode='-Odt'):
=======
    def open_experiment_data(self, file_name=None):
        """Open and associate an experiment file.
        If file_name is None, query MASsoft for the current filename."""
        # 1) Determine the filename string
        if file_name is None:
            full_path = self.query_filename_data()
        else:
            # Coerce list or tuple into a single string
            if isinstance(file_name, (list, tuple)):
                file_name = file_name[0]
            # Build a pure-Windows path
            full_path = str(PureWindowsPath(EXPERIMENT_DIRECTORY) / file_name)

        # 2) Send to MASsoft
        resp = self.data_socket.send_command(f'-f"{full_path}"')
        if resp =='0':
            raise RuntimeError(f"Failed to open experiment file: {full_path}")

        # 3) Remember it for future operations
        self.current_file = full_path
        return full_path

    def open_experiment_status(self, file_name=None):
        """Open and associate an experiment file.
        If file_name is None, query MASsoft for the current filename."""
        # 1) Determine the filename string
        if file_name is None:
            full_path = self.query_filename()
        else:
            # Coerce list or tuple into a single string
            if isinstance(file_name, (list, tuple)):
                file_name = file_name[0]
            # Build a pure-Windows path
            full_path = str(PureWindowsPath(EXPERIMENT_DIRECTORY) / file_name)

        # 2) Send to MASsoft
        resp = self.status_socket_socket.send_command(f'-f"{full_path}"')
        if resp =='0':
            raise RuntimeError(f"Failed to open experiment file: {full_path}")

        # 3) Remember it for future operations
        self.current_file = full_path
        return full_path

    def run_experiment(self, new_file_name = None, mode = "-Odt"):
        """Start the experiment."""        
>>>>>>> 0c250acc164651cb67d67172c79817375ce6ba28
        resp = self.command_socket.send_command(f'-xGo {mode}')
        if resp == '0':
            raise RuntimeError("Experiment failed to start.")
        if not resp:
            logging.warning("Assuming experiment started despite no response.")

    def associate_status_link(self, view=1):
        if not self.current_file:
            raise RuntimeError("No file opened.")
        self.open_experiment_status()
        self.status_socket.send_command(f'-lStatus -v{view}')

    def monitor_until_stopped(self, timeout=120):
        if not self.current_file:
            raise RuntimeError("No file opened.")
        self.associate_status_link()
        start = time.time()
        while time.time() - start < timeout:
            status = self.status_socket.receive()
            if status:
                logging.info(f"Status: {status}")
                if status.lower().startswith('stopped'):
                    return True
            time.sleep(1)
        raise TimeoutError(f"Did not stop within {timeout}s.")

    def get_data(self, view=1, cycles=1, block=False, as_text=False, timeout=10):
        """Retrieve scan data.

        Args:
            view: view number to request.
            cycles: number of cycles (rows) to collect before returning.
            block: if True, wait up to `timeout` seconds to collect `cycles` rows; if False, return whatever is available.
            as_text: if True, do not convert values to float.
            timeout: maximum seconds to wait when `block` is True.

        Returns:
            list of rows (each row is a list of values or floats depending on `as_text`).
        """
        if not self.current_file:
            raise RuntimeError("No file opened.")

        start = time.time()
        data = []
        while True:
            raw_data = self.data_socket.send_command(f"-lData -v{view}")
            if raw_data and raw_data != '0':
                lines = raw_data.strip().split('\r\n')
                for line in lines:
                    if not line or line.strip() == '0':
                        continue
                    values = line.split()
                    if not as_text:
                        parsed = []
                        for v in values:
                            try:
                                parsed.append(float(v))
                            except Exception:
                                parsed.append(v)
                        values = parsed
                    data.append(values)
                    if cycles and len(data) >= cycles:
                        return data

            if not block:
                return data

            if time.time() - start > timeout:
                logging.warning("get_data timed out waiting for cycles")
                return data

            time.sleep(0.2)

    def get_legends(self, view=1):
        path = self.command_socket.send_command("-xFilename")
<<<<<<< HEAD
        if not path:
            raise RuntimeError("Failed to get filename for legends")
        self.command_socket.send_command(f'-f"{path}"')
        start = time.time()
        while True:
            raw_data = self.command_socket.send_command(f"-lLegends -v{view}")
            if raw_data and raw_data != '0':
                legend = raw_data.replace("\r\n", "\t").split("\t")
                return legend
            if time.time() - start > 10:
                raise TimeoutError("Timeout retrieving legends")
            time.sleep(0.2)
=======
        time.sleep(1)
        path = self.command_socket.send_command("-xFilename")
        self.command_socket.send_command(f'-f"{path}"')
        try:
            while True:
                raw_data = self.command_socket.send_command(f"-lLegends -v{view}")
                if raw_data != '0':
                    legend = raw_data.replace("\r\n", "\t").split("\t")
                    break
                else:
                    time.sleep(1)                
        except KeyboardInterrupt:
            print("Done.")
        return legend, path

    def get_legends_data(self, view=1):
        """Retrieve column legends via a temporary socket."""
        path = self.data_socket.send_command("-xFilename")
        time.sleep(1)
        path = self.data_socket.send_command("-xFilename")
        self.command_socket.send_command(f'-f"{path}"')
        try:
            while True:
                raw_data = self.command_socket.send_command(f"-lLegends -v{view}")
                if raw_data != '0':
                    legend = raw_data.replace("\r\n", "\t").split("\t")
                    break
                else:
                    time.sleep(1)
        except KeyboardInterrupt:
            print("Done.")
        return legend_data
>>>>>>> 0c250acc164651cb67d67172c79817375ce6ba28

    def query_filename(self):
        resp = self.command_socket.send_command('-xFilename')
        if resp == '0':
            raise RuntimeError("Failed querying filename.")
        return resp

    def query_filename_data(self):
        """Return the filename currently associated with the command socket."""
        resp = self.data_socket.send_command('-xFilename')
        if resp == '0':
            raise RuntimeError("Failed querying filename.")
        return resp

    def close_experiment(self):
<<<<<<< HEAD
        try:
            resp = self.command_socket.send_command('-xClose')
        except Exception as ex:
            logging.warning(f"close_experiment error: {ex}")
            return
        if resp not in ('1', ''):
            logging.warning("Close experiment returned unexpected response")

    def abort_experiment(self):
        try:
            resp = self.command_socket.send_command('-xAbort')
        except Exception as ex:
            logging.warning(f"abort_experiment error: {ex}")
            return
        if resp not in ('1', ''):
            logging.warning("Abort returned unexpected response")
=======
        """Close the experiment file."""
        resp = self.command_socket.send_command('-xClose')
        if resp == '0':
            raise RuntimeError("Close failed.")
        
    def abort_experiment(self):
        """Abort the experiment."""
        resp = self.command_socket.send_command('-xAbort')
        if resp == '0':
            raise RuntimeError("Abort failed.")
>>>>>>> 0c250acc164651cb67d67172c79817375ce6ba28

    def shutdown(self):
        self.command_socket.close()
        self.status_socket.close()
        self.data_socket.close()


# Example IPython Usage:
# from massoft_client import MASsoftClient
# client = MASsoftClient(); client.initialize()
# client.open_experiment('file56.exp')
# client.run_experiment(); client.associate_status_link()
# client.monitor_until_stopped(timeout=300)
# print(client.query_filename())
# data = client.get_data(); legends = client.get_legends()
# client.close_experiment(); client.shutdown()
