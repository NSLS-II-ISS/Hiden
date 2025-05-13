import socket
import time
import pandas as pd



class HidenHPR20Interface:
    def __init__(self, file_name=None, view=None):
        self.file_name = file_name
        self.view = view
        # self.file_path = r'C:\Users\08id-user\Documents\Hiden Analytical\MASsoft\11'
        # self.full_path = os.path.join(self.file_path, self.file_name)
        self.full_path = "HIDEN_LastFile"
        self.host = '10.66.58.225'
        self.port = 5026
        self.out_terminator = "\r\n"
        self.in_terminator = "\r\n"
        self.data_sock = None

    # Establish a socket connection
    def open_socket(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            print("Socket connected.")
            # Send a dummy status check to clear any initial data
            self.send_command('-xStatus')  # Ignore the first response
        except Exception as e:
            print(f"Failed to connect: {e}")
            self.sock = None

    # Close the main socket connection
    def close_socket(self):
        if self.sock:
            self.sock.close()
            print("Socket closed.")
        if self.data_sock:
            self.data_sock.close()
            print("Data socket closed.")

    # Send a command through the socket
    def send_command(self, command):
        try:
            self.sock.sendall((command + self.out_terminator).encode())
            response = self.sock.recv(4096)
            decoded_response = response.decode().strip()
            return decoded_response
        except Exception as e:
            print(f"Failed to send command: {e}")
            return None

        # Open the file and run the experiment

    def open_file(self):
        if self.sock:
            response = self.send_command(f'-f "{self.full_path}" -d20')
            time.sleep(1)
            response = self.send_command(f'-f "{self.full_path}" -d20')
            print(f"File Open Response: {response}")
            if response == '1':  # File opened successfully
                # response = self.send_command('-xGo -odt -d20')
                response = 'Open'
                print(f"File {response}")
            else:
                print("Failed to open file.")
        else:
            print("Socket not connected.")

    def close_file(self):
        if self.sock:
            response = self.send_command('-xClose -d20')
            print(f"Close File Response: {response}")
        else:
            print("Socket not connected.")

    # Get the current filename associated with the socket
    def get_filename(self):
        self.open_socket()
        if self.sock:
            response = self.send_command('-xFilename')
            print(f"Current Filename: {response}")
        else:
            print("Socket not connected.")

    def parse_data(self, view_num, data):
        # Split the received data into lines
        lines = data.strip().split('\n')
        parsed_data = []
        self.open_socket()
        headers = self.data_headers(view_num)
        time.sleep(1)
        headers = self.data_headers(view_num)
        for line in lines:
            # Ignore the first line if it only contains '0'
            if line.strip() == '0':
                print("Ignoring first line with '0'.")
                continue
            values = line.split()
            # Check if the number of parsed values is less than expected
            if len(values) < len(headers):
                print(f"Line skipped due to insufficient values: {line.strip()}")
                continue  # Skip this line if it doesn't have enough values
            # If line is valid, append to parsed_data
            parsed_data.append(values)
        # Convert parsed_data to a DataFrame, if there's data
        if parsed_data:
            df = pd.DataFrame(parsed_data, columns=headers)
            return df
        else:
            print("No data parsed.")
            return pd.DataFrame()  # Return an empty DataFrame if no data

    def scan_parameters(self, view_num):
        self.open_socket()
        self.open_file()
        try:
            while True:
                raw_data = self.send_command(f"-lScanParameters -v{view_num} -d20")
                print(raw_data)
                time.sleep(1)
                raw_data = self.send_command(f"-lScanParameters -v{view_num} -d20")
                print(raw_data)
                if raw_data != '0':
                    data_stripped = raw_data.replace("\r\n", "\t").split("\t")
                    print(data_stripped)
                    headers = data_stripped[:11]
                    print(headers)
                    rows = [data_stripped[i:i + 11] for i in range(11, len(data_stripped), 11)]
                    print(rows)
                    data_dict = {header: [] for header in headers}
                    for row in rows:
                        for i, header in enumerate(headers):
                            data_dict[header].append(row[i])
                    print(data_dict)
                    break
                else:
                    time.sleep(1)
        except KeyboardInterrupt:
            print("Done.")
        self.close_socket()
        return data_dict

    def data_headers(self, view_num):
        self.open_socket()
        try:
            while True:
                raw_data = self.send_command(f"-lLegends -v{view_num} -d20")
                time.sleep(1)
                raw_data = self.send_command(f"-lLegends -v{view_num} -d20")
                if raw_data != '0':
                    data_stripped = raw_data.replace("\r\n", "\t").split("\t")
                    break
                else:
                    time.sleep(1)
        except KeyboardInterrupt:
            print("Done.")
        self.close_socket()
        return data_stripped

    def data_collecting_loop(self, view_num):
        headers = self.data_headers(view_num)
        print(f'Collecting {headers}')
        self.open_socket()
        self.open_file()
        parsed_data = []
        while True:
            raw_data = self.send_command(f"-lData -v{view_num}")
            if raw_data != '0':

                lines = raw_data.strip().split('\r\n')
                print(f'Lines: {lines}')

                for line in lines:
                    if line.strip() == '0':
                        print("Ignoring first line with '0'.")
                        continue
                    values = line.split()
                    print(f'Values: {values}')
                    if len(values) < len(headers):
                        print(f"Line skipped due to insufficient values: {line.strip()}")
                        continue
                    parsed_data.append(values)

                if parsed_data:
                    # Convert parsed data to DataFrame
                    df = pd.DataFrame(parsed_data, columns=headers)
                    print(df)
            time.sleep(5)


#!/usr/bin/env python3
import asyncio
import random
import logging

from caproto.server import pvproperty, PVGroup, ioc_arg_parser, run

logging.basicConfig(level=logging.INFO)




class RGAIOC(PVGroup):

    # Control PV to start/stop the acquisition loop
    initialize = pvproperty(
        name='XF:08IDB-SE{{RGA:1}}:OpenExp',
        value=0,
        doc='Configure the experiment',
        dtype=int,
    )

    experiment = pvproperty(
        name='XF:08IDB-SE{{RGA:1}}:ExpName',
        value='file1.exp',
        dtype=str,
        max_length=40
    )

    acquire = pvproperty(
        name='XF:08IDB-SE{{RGA:1}}:Acquire',
        value=0,
        doc='Start/stop the acquisition loop',
        dtype=int,
    )

    # Define the ten MID‑I readback PVs
    for idx in range(1, 11):
        locals()[f'mid{idx}'] = pvproperty(
            name=f'XF:08IDB-SE{{{{RGA:1}}}}P:MID{idx}-I',
            value=0.0,
            doc=f'RGA reading for MID{idx}',
        )
    del idx  # clean up namespace

    # Generate one Mass PV per idx
    for idx in range(1, 11):
        # attribute name is now mass1, mass2, … massN
        locals()[f'mass{idx}'] = pvproperty(
            name=f'XF:08IDB-VA{{{{RGA:1}}}}Mass:MID{idx}',
            value=0.0,
            doc=f'RGA mass PV for MID{idx}',
        )
    del idx  # clean up namespace


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rga_device = HidenHPR20Interface(1)
        self.pv_count = 11
        self._running = False
        self._task = None

    @acquire.putter
    async def acquire(self, instance, value):
        """Triggered when someone writes to the START PV."""
        want_acquire = bool(int(value))
        if want_acquire and not self._running:
            logging.info("Starting acquisition loop")
            self._running = True
            # spawn background task
            self._task = asyncio.create_task(self._acquire_loop())
        elif not want_acquire and self._running:
            logging.info("Stopping acquisition loop")
            self._running = False
            if self._task:
                self._task.cancel()
        return value

    async def _acquire_loop(self):
        """Read all channels once per second until stopped."""
        try:
            # Parses RGA data headers into PVs
            headers = self.rga_device.data_headers(1)
            mass_values = [
                float(h.split()[-1])
                for h in headers
                if 'mass' in h
            ]
            print('Mass values: {}'.format(mass_values))
            for idx, mass_val in enumerate(mass_values[:10], start=1):
                print(f'Mass {idx}: {mass_val}')
                pv = getattr(self, f'mass{idx}')
                await pv.write(mass_val)
                logging.debug(f"Wrote {mass_val:.2f} to {pv.name}")


            self.rga_device.open_socket()
            self.rga_device.open_file()


            while self._running:
                print(self._running)
                print('Trying......')
                try:
                    raw_data = self.rga_device.send_command(f"-lData -v1")
                    if raw_data != '0':
                        lines = raw_data.strip().split('\r\n')
                        for line in lines:
                            if line.strip() == '0':
                                print("Ignoring first line with '0'.")
                                continue
                            values = line.split()[2:]
                            print(f'Values: {values}')
                            print(f'Length of values: {len(values)}')
                            print('Length of mass values: {}'.format(len(mass_values)))
                            if len(values) == len(mass_values):
                                print('updating PVs')
                                for idx, val in enumerate(values):
                                    print(f'Index {idx}: {val}')
                                    pv = getattr(self, f'mid{idx+1}')
                                    print(pv)
                                    await pv.write(float(val))
                                    logging.debug(f'Wrote {val} to {pv.name}')
                except Exception as e:
                    print(f"Caught an exception: {e}")

                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            logging.info("Acquisition loop cancelled")
            return


if __name__ == '__main__':
    ioc_options, run_options = ioc_arg_parser(
        default_prefix='',  # we've given full names explicitly
        desc='Caproto IOC for RGA with START trigger'
    )
    ioc = RGAIOC(**ioc_options)
    run(ioc.pvdb, **run_options)
