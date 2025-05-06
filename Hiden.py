import socket
import threading
import os
import time
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.animation import FuncAnimation
import h5py

class HidenHPR20Interface:
    def __init__(self, file_name = None, view = None):
        self.file_name = file_name
        self.view = view
        self.file_path = r'C:\Users\jmoncadav\OneDrive - Brookhaven National Laboratory\Documents\Hiden Analytical\MASsoft\11'
        self.full_path = os.path.join(self.file_path, self.file_name)
        self.host = 'localhost'
        self.port = 5026
        self.out_terminator = "\r\n"
        self.in_terminator = "\r\n"
        self.fig, self.ax = plt.subplots()
        self.full_dataset = pd.DataFrame()
        self.ani = None
        self.sock = None
        self.data_sock = None
        plt.ion()

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

    # def data_collecting_loop(self, view_num):
    #     all_data = ""
    #     try:
    #         while True:
    #             raw_data = self.send_command(f"-lData -v{view_num}")
    #             if raw_data != '0':
    #                 print(f"Collecting .... {raw_data}")
    #                 all_data += raw_data + "\r\n "
    #             time.sleep(5)
                
    #     except KeyboardInterrupt:
    #         print("Done.")
    #         print(self.parse_data(view_num, all_data))

    # def data_collecting_loop(self, view_num):
    #     self.open_socket()
    #     self.open_file()
    #     parsed_data = []
    #     try:
    #         while True:
    #             raw_data = self.send_command(f"-lData -v{view_num}")
    #             if raw_data != '0':
    #                 print(f"Collecting .... {raw_data}")
    #                 lines = raw_data.strip().split('\n')
    #                 for line in lines:
    #                     # Ignore the first line if it only contains '0'
    #                     if line.strip() == '0':
    #                         print("Ignoring first line with '0'.")
    #                         continue
    #                     # print(f"Parsing line: {line.strip()}")
    #                     values = line.split()
    #                     if len(values) < 10:
    #                         print(f"Line skipped due to insufficient values: {line.strip()}")
    #                         continue  # Skip this line if it doesn't have enough values
    #                     parsed_data.append(values)

    #                 if parsed_data:
    #                     df = pd.DataFrame(parsed_data)
    #                     print(df)
    #                 else:
    #                     print("No data parsed.")
    #                     return pd.DataFrame()  # Return an empty DataFrame if no data
    #             time.sleep(5)
                
    #     except KeyboardInterrupt:
    #         print(parsed_data)
    #         # print(self.parse_data(view_num, all_data))

    def data_collecting_loop(self, view_num, hdf5_file='data3.h5', dataset_name='mass_spectrometer_data'):
        headers = self.data_headers2(view_num)
        self.open_socket()
        self.open_file()
        parsed_data = []

        # Open HDF5 file in append mode
        with pd.HDFStore(hdf5_file, mode='a') as store:
            try:
                while True:
                    raw_data = self.send_command(f"-lData -v{view_num}")
                    if raw_data != '0':
                        print(raw_data)
                        lines = raw_data.strip().split('\r\n')
                        for line in lines:
                            if line.strip() == '0':
                                print("Ignoring first line with '0'.")
                                continue
                            values = line.split()
                            if len(values) < 10:
                                print(f"Line skipped due to insufficient values: {line.strip()}")
                                continue

                            parsed_data.append(values)

                        if parsed_data:
                            # Convert parsed data to DataFrame
                            df = pd.DataFrame(parsed_data, columns=headers)

                            # Append to HDF5 file
                            df.to_hdf(store, key=dataset_name, format='table', append=True, index=False)
                            print(df)
                            
                            # Reset parsed_data to avoid appending duplicate data
                            parsed_data = []

                    time.sleep(5)
                    
            except KeyboardInterrupt:
                print("Data collection stopped.")
                print(parsed_data)
            except Exception as e:
                print(f"An error occurred: {e}")
    def data_headers(self, view_num):
        try:
            while True:
                raw_data = self.send_command(f"-lLegends -v{view_num} -d20")
                if raw_data != '0':
                    data_stripped = raw_data.strip().split('\t')
                    # print(data_stripped)
                    # print('Items in list: ', len(data_stripped))
                    break
                else:
                    time.sleep(1)
                
        except KeyboardInterrupt:
            print("Done.")

        return data_stripped


    # Send a command through the socket
    def send_command(self, command):
        try:
            self.sock.sendall((command + self.out_terminator).encode())
            response = self.sock.recv(4096)
            # print(f"Raw Response: {response}")  # Print raw response for debugging
            decoded_response = response.decode().strip()
            # print(decoded_response)
            # split_response = decoded_response.split("\t")
            # print(split_response)
            return decoded_response
        except Exception as e:
            print(f"Failed to send command: {e}")
            return None
    
    def scan_parameters(self, view_num):
        self.open_socket()
        try:
            while True:
                raw_data = self.send_command(f"-lScanParameters -v{view_num} -d20")
                time.sleep(1)
                raw_data = self.send_command(f"-lScanParameters -v{view_num} -d20")
                if raw_data != '0':
                    data_stripped = raw_data.replace("\r\n", "\t").split("\t")
                    headers = data_stripped[:11]
                    rows = [data_stripped[i:i+11] for i in range(11, len(data_stripped), 11)]
                    data_dict = {header: [] for header in headers}
                    for row in rows:
                        for i, header in enumerate(headers):
                            data_dict[header].append(row[i])
                    ## Print number of items in table
                    # first_key = list(data_dict.keys())[0]
                    # item_count = len(data_dict[first_key])
                    # print(f"The first key '{first_key}' has {item_count} items.")
                    ## Print a new dictionary with values for Scan "n"
                    # scanpar1 = {key: value[0] for key, value in scanpar.items()}
                    # print(scanpar1)
                    break
                else:
                    time.sleep(1)                
        except KeyboardInterrupt:
            print("Done.")
        self.close_socket()
        return data_dict
    
    def data_headers2(self, view_num):
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

    def parse_data(self, view_num, data):
        # print("Raw Data Received:")
        # print(data)

        # Split the received data into lines
        lines = data.strip().split('\n')
        parsed_data = []

        self.open_socket()
        headers = self.data_headers(view_num)
        time.sleep(1)
        headers = self.data_headers(view_num)
        # print(headers)
        # print(len(headers))

        # Iterate through the lines
        for line in lines:
            # Ignore the first line if it only contains '0'
            if line.strip() == '0':
                print("Ignoring first line with '0'.")
                continue

            # print(f"Parsing line: {line.strip()}")
            values = line.split()
            # print(f"Parsed values: {values}")

            # Check if the number of parsed values is less than expected
            # print('values: ', len(values))
            # print('columns: ', len(headers))
            if len(values) < len(headers):
                print(f"Line skipped due to insufficient values: {line.strip()}")
                continue  # Skip this line if it doesn't have enough values

            # If line is valid, append to parsed_data
            parsed_data.append(values)

        # Convert parsed_data to a DataFrame, if there's data
        if parsed_data:
            df = pd.DataFrame(parsed_data, columns=headers)

            # Combine 'Time' and 'Milliseconds' to create a more precise timestamp
            # df['Elapsed time'] = pd.to_datetime(df['Elapsed time'], format='%H:%M:%S') + pd.to_timedelta(df['Time (ms)'].astype(int), unit='ms')
            return df
        else:
            print("No data parsed.")
            return pd.DataFrame()  # Return an empty DataFrame if no data


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

    # Monitor the status of the MSIU
    def monitor_status(self):
        status_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            status_sock.connect((self.host, self.port))
            response = self.send_command(f'-f "{self.full_path}" -d20')
            print(f"Status Socket File Association: {response}")
            response = self.send_command('-xStatus -d20')
            print(f"Status Hotlink Response: {response}")

            while True:
                status = status_sock.recv(1024).decode().strip()
                if status:
                    print(f"Status: {status}")
                    if "StoppedShutdown" in status:
                        break  # Experiment stopped, exit loop
        except Exception as e:
            print(f"Failed to monitor status: {e}")
        finally:
            status_sock.close()

    # Real-time data retrieval in a separate thread
    def data_thread(self, view_num):
        self.data_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.data_sock.connect((self.host, self.port))
            print("Data socket connected.")
        except Exception as e:
            print(f"Failed to connect data socket: {e}")
            return

        if self.full_path:
            # Associate the socket with the open file
            response = self.send_command(f'-f "{self.full_path}" -d20')
            time.sleep(1)
            response = self.send_command(f'-f "{self.full_path}" -d20')
            print(f"Data Thread File Association: {response}")
            
            # Create a data hotlink
            response = self.send_command(f'-lData -v{view_num} -d20')
            print(f"Data Hotlink Response: {response}")
            
            # Continuously receive data
            while True:
                try:
                    data = self.data_sock.recv(1024).decode().strip()
                    if data:
                        print(f"Data: {data}")
                        self.update_plot(data)
                except:
                    break
        self.data_sock.close()

    # Start the data thread to retrieve data in real-time
    def start_data_thread(self):
        data_thread = threading.Thread(target=self.data_thread)
        data_thread.start()

    # Update the plot in real-time
    def update_plot(self, new_data):
        self.x_data.append(time.time())  # Assuming time as x-axis
        self.y_data.append(float(new_data))  # New data for y-axis
        
        plt.clf()  # Clear the current figure
        plt.plot(self.x_data, self.y_data)
        plt.pause(0.05)  # Pause to allow the plot to update

    # Get the current filename associated with the socket
    def get_filename(self):
        if self.sock:
            response = self.send_command('-xFilename')
            print(f"Current Filename: {response}")
        else:
            print("Socket not connected.")

    # Set a logical device value using LSet command
    def set_logical_device(self, device_name, value):
        if self.sock:
            command = f'-xLSet {device_name} {value} -v1'
            response = self.send_command(command)
            print(f"Logical Device Set Response: {response}")
        else:
            print("Socket not connected.")

    # Export the acquired data
    def export_data(self, view=1):
        if self.sock:
            command = f'-xExport -v{view}'
            response = self.send_command(command)
            print(f"Data Export Response: {response}")
        else:
            print("Socket not connected.")

    # Close the main socket connection
    def close_socket(self):
        if self.sock:
            self.sock.close()
            print("Socket closed.")
        if self.data_sock:
            self.data_sock.close()
            print("Data socket closed.")

# Example usage
if __name__ == "__main__":
    # Initialize the interface
    hiden_interface = HidenHPR20Interface()

    # Establish the connection
    hiden_interface.open_socket()

    # Open the file and run the experiment
    hiden_interface.open_file(r'C:\Users\jmoncadav\OneDrive - Brookhaven National Laboratory\Documents\Hiden Analytical\MASsoft\11\postbaking_test1.exp')

    # Start retrieving data in real-time
    hiden_interface.start_data_thread()

    # Monitor status in a separate thread
    threading.Thread(target=hiden_interface.monitor_status).start()

    # Simulate for 60 seconds (replace with actual experiment duration)
    time.sleep(60)

    # Export the data from view 1
    hiden_interface.export_data(view=1)

    # Get the current filename
    hiden_interface.get_filename()

    # Close the file and socket when done
    hiden_interface.close_file()
    hiden_interface.close_socket()
