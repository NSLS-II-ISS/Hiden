import asyncio
import logging
import os

try:
    import nest_asyncio
    nest_asyncio.apply()
except ImportError:
    pass

# Logger setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("massoft_async.log")]
)

# Configuration
MAS_HOST = '10.66.58.225'
MAS_PORT = 5026
EXPERIMENT_DIRECTORY = r"C:\Users\08id-user\Documents\Hiden Analytical\MASsoft\11"
RETRY_DELAY = 20  # appended as -d20

class AsyncMASsoftSocket:
    def __init__(self, name: str):
        self.name = name
        self.reader: asyncio.StreamReader = None
        self.writer: asyncio.StreamWriter = None

    async def connect(self):
        if self.writer and not self.writer.is_closing():
            return
        self.reader, self.writer = await asyncio.open_connection(MAS_HOST, MAS_PORT)
        logging.info(f"{self.name} connected to {MAS_HOST}:{MAS_PORT}")
        # discard greeting line
        await self.reader.readline()

    async def send_command(self, cmd: str, expect_response: bool = True) -> str:
        await self.connect()
        full_cmd = f"{cmd.strip()} -d{RETRY_DELAY}\r\n"
        self.writer.write(full_cmd.encode('utf-8'))
        await self.writer.drain()
        if not expect_response:
            return ''
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout=5.0)
            resp = line.decode('utf-8').strip()
            logging.info(f"{self.name} | Cmd: {full_cmd.strip()} | Resp: {resp}")
            return resp
        except asyncio.TimeoutError:
            logging.warning(f"{self.name} response timeout for: {full_cmd.strip()}")
            return ''

    async def receive(self) -> str:
        await self.connect()
        try:
            line = await asyncio.wait_for(self.reader.readline(), timeout=1.0)
            return line.decode('utf-8').strip()
        except asyncio.TimeoutError:
            raise

    def close(self):
        if self.writer and not self.writer.is_closing():
            self.writer.close()
            logging.info(f"{self.name} closed")

class AsyncMASsoftClient:
    def __init__(self):
        self.cmd_sock  = AsyncMASsoftSocket("CmdSocket")
        self.stat_sock = AsyncMASsoftSocket("StatSocket")
        self.data_sock = AsyncMASsoftSocket("DataSocket")
        self.current_file: str = ""

    async def initialize(self):
        await asyncio.gather(
            self.cmd_sock.connect(),
            self.stat_sock.connect(),
            self.data_sock.connect()
        )

    async def open_experiment(self, file_name: str):
        path = os.path.join(EXPERIMENT_DIRECTORY, file_name)
        resp = await self.cmd_sock.send_command(f'-f"{path}"')
        if resp in ('0', ''):
            raise RuntimeError(f"Failed to open experiment file: {path}")
        self.current_file = path

    async def run_experiment(self, mode: str = '-Odt', view: int = 1, verify_timeout: int = 30):
        if not self.current_file:
            raise RuntimeError("No experiment file opened")
        resp = await self.cmd_sock.send_command(f'-xGo {mode}')
        if resp == '0':
            raise RuntimeError("MASsoft returned failure to -xGo")
        # link status socket
        await self.stat_sock.send_command(f'-f"{self.current_file}"')
        await self.stat_sock.send_command(f'-lStatus -v{view}')
        # wait for actual start
        deadline = asyncio.get_event_loop().time() + verify_timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                status = (await self.stat_sock.receive()).lower()
            except asyncio.TimeoutError:
                continue
            logging.info(f"Startup Status: {status}")
            if 'startingactive' in status or 'scanningactive' in status:
                logging.info("Experiment confirmed running")
                return
        raise TimeoutError(f"No start detected within {verify_timeout}s")

    async def monitor_until_stopped(self, timeout: int = 120):
        if not self.current_file:
            raise RuntimeError("No experiment file opened")
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                status = await asyncio.wait_for(self.stat_sock.receive(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            logging.info(f"Status Update: {status}")
            if status.lower().startswith('stopped'):
                return
        raise TimeoutError(f"Did not stop within {timeout}s")

    async def get_data(self, view: int = 1, cycles: int = None, time_fmt: bool = False, ms_fmt: bool = False):
        if not self.current_file:
            raise RuntimeError("No experiment file opened")
        await self.data_sock.send_command(f'-f"{self.current_file}"')
        cmd = f'-lData -v{view}'
        if cycles is not None: cmd += f' -c{cycles}'
        cmd += f' -t{1 if time_fmt else 0}'
        cmd += f' -m{1 if ms_fmt else 0}'
        await self.data_sock.send_command(cmd)
        results = []
        while True:
            try:
                line = await self.data_sock.receive()
            except asyncio.TimeoutError:
                break
            parts = line.split()
            if len(parts) < 2:
                continue
            results.append(parts if (time_fmt or ms_fmt) else [float(p) for p in parts])
        return results

    async def get_legends(self, view: int = 1):
        if not self.current_file:
            raise RuntimeError("No experiment file opened")
        resp = await self.cmd_sock.send_command(f'-lLegends -v{view}')
        return [item.strip('"') for item in resp.split()]

    async def query_filename(self):
        resp = await self.cmd_sock.send_command('-xFilename')
        if resp in ('0', ''):
            raise RuntimeError("Failed to retrieve filename")
        return resp

    async def close_experiment(self):
        resp = await self.cmd_sock.send_command('-xClose')
        if resp != '1':
            raise RuntimeError("Failed to close experiment file")

    async def shutdown(self):
        self.cmd_sock.close()
        self.stat_sock.close()
        self.data_sock.close()

async def main_workflow():
    client = AsyncMASsoftClient()
    await client.initialize()
    await client.open_experiment("file56.exp")

    run_task     = asyncio.create_task(client.run_experiment(verify_timeout=45))
    monitor_task = asyncio.create_task(client.monitor_until_stopped(timeout=300))
    data_task    = asyncio.create_task(client.get_data(view=1))

    await asyncio.gather(run_task, monitor_task, data_task)

    data     = data_task.result()
    legends  = await client.get_legends()
    filename = await client.query_filename()

    print("Legends:", legends)
    print(f"Pulled {len(data)} data points from {filename!r}")

    await client.close_experiment()
    await client.shutdown()

# In Jupyter/IPython, simply do:
# await main_workflow()
