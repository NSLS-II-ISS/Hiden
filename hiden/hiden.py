import socket
import time
import pandas as pd


class HidenHPR20Interface:
    def __init__(self, file_name = None, view = None):
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
                    rows = [data_stripped[i:i+11] for i in range(11, len(data_stripped), 11)]
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


    

    

    



    # #WIP
    # # Monitor the status of the MSIU
    # def monitor_status(self):
    #     status_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    #     try:
    #         status_sock.connect((self.host, self.port))
    #         response = self.send_command(f'-f "{self.full_path}" -d20')
    #         print(f"Status Socket File Association: {response}")
    #         response = self.send_command('-xStatus -d20')
    #         print(f"Status Hotlink Response: {response}")

    #         while True:
    #             status = status_sock.recv(1024).decode().strip()
    #             if status:
    #                 print(f"Status: {status}")
    #                 if "StoppedShutdown" in status:
    #                     break  # Experiment stopped, exit loop
    #     except Exception as e:
    #         print(f"Failed to monitor status: {e}")
    #     finally:
    #         status_sock.close()

    # # Real-time data retrieval in a separate thread
    # def data_thread(self, view_num):
    #     self.data_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    #     try:
    #         self.data_sock.connect((self.host, self.port))
    #         print("Data socket connected.")
    #     except Exception as e:
    #         print(f"Failed to connect data socket: {e}")
    #         return

    #     if self.full_path:
    #         # Associate the socket with the open file
    #         response = self.send_command(f'-f "{self.full_path}" -d20')
    #         time.sleep(1)
    #         response = self.send_command(f'-f "{self.full_path}" -d20')
    #         print(f"Data Thread File Association: {response}")
            
    #         # Create a data hotlink
    #         response = self.send_command(f'-lData -v{view_num} -d20')
    #         print(f"Data Hotlink Response: {response}")
            
    #         # Continuously receive data
    #         while True:
    #             try:
    #                 data = self.data_sock.recv(1024).decode().strip()
    #                 if data:
    #                     print(f"Data: {data}")
    #                     self.update_plot(data)
    #             except:
    #                 break
    #     self.data_sock.close()

    # # Start the data thread to retrieve data in real-time
    # def start_data_thread(self):
    #     data_thread = threading.Thread(target=self.data_thread)
    #     data_thread.start()

    # # Update the plot in real-time
    # def update_plot(self, new_data):
    #     self.x_data.append(time.time())  # Assuming time as x-axis
    #     self.y_data.append(float(new_data))  # New data for y-axis
        
    #     plt.clf()  # Clear the current figure
    #     plt.plot(self.x_data, self.y_data)
    #     plt.pause(0.05)  # Pause to allow the plot to update



    # # Set a logical device value using LSet command
    # def set_logical_device(self, device_name, value):
    #     if self.sock:
    #         command = f'-xLSet {device_name} {value} -v1'
    #         response = self.send_command(command)
    #         print(f"Logical Device Set Response: {response}")
    #     else:
    #         print("Socket not connected.")

    # # Export the acquired data
    # def export_data(self, view=1):
    #     if self.sock:
    #         command = f'-xExport -v{view}'
    #         response = self.send_command(command)
    #         print(f"Data Export Response: {response}")
    #     else:
    #         print("Socket not connected.")



## Example usage
# from hiden_interface import HidenHPR20Interface
# hiden = HidenHPR20Interface("file53.exp", 1)
# hiden.data_headers(1)
# hiden.data_collecting_loop(1)