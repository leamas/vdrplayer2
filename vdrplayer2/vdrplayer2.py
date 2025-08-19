#!/usr/bin/env python3

#  Copyright (C) 2025 Alec Leamas
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Tool to replay log files created by the OpenCPN Data Monitor in
VDR mode.

Use vdrplayer2 -h for help

Kudos: Dan Dickey a k a Transmitterdan for the original VDRplayer.py script
at https://github.com/transmitterdan/VDRplayer.git which has been the
inspiration for this.
"""

import argparse
import asyncio
import csv
import socket
import sys
import time
import websockets

from websockets.asyncio.server import serve

assert sys.version_info >= (3, 10), "Must run in Python version 3.10 or above"


class ProgressPrinter:
    """Report processed lines on stdout."""

    def __init__(self, args, total):
        """Count number of lines in file and reset stream"""
        self.timestamp = 0
        self.total = total
        self.quiet = args.quiet

    def report(self, lines, force=False):
        """Print a written/total string if last is deemed too old."""
        if self.quiet:
            return
        elapsed = time.time() - self.timestamp
        if force or elapsed > 1:
            print(f"Processed {lines}/{self.total}\r", end="", flush=True)
            self.timestamp = time.time()


class LogFileReader:
    """Iterable file object, removes comments and empty lines from source given
    to open()."""

    def __iter__(self):
        return self

    def __next__(self):
        line = self.read()
        if not line:
            raise StopIteration
        return line

    def open(self, text_io_wrapper):
        """Start reading from given iterable argument, typically returned by
        open()"""
        # pylint: disable=attribute-defined-outside-init

        self.text_io_wrapper = text_io_wrapper

    def read(self):
        """Standard file object operation"""
        line = self.text_io_wrapper.readline()
        while line and (not line.strip() or line.strip().startswith("#")):
            line = self.text_io_wrapper.readline()
        return line.strip()

    def count_lines(self):
        """Return number of data (not comments etc) lines"""

        lines = 0
        for _ in self:
            lines += 1
        self.text_io_wrapper.seek(0)
        return lines - 1


class MessageReader:
    """Iterable object, remove all rows created by csv.DictReader which
    does not match message type given to constructor. Add delays
    corresponding to timestamps"""

    def __init__(self, source, message_type):
        self.message_type = message_type
        self.source = source
        self.timestamp = -1

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            row = self.source.__next__()
            if not row.keys() & {"received_at", "protocol"}:
                print("Bad row: " + str(row), file=sys.stderr)
                continue
            if self.message_type not in row["protocol"].lower():
                continue
            try:
                timestamp = float(row["received_at"]) / 1000
            except ValueError:
                print("Bad timestamp: " + row["received_at"], file=sys.stderr)
            else:
                if self.timestamp != -1:
                    delay = timestamp - self.timestamp
                    time.sleep(delay)
                self.timestamp = timestamp
                return row


class MsgFormat:
    """Format rows found in log file to their original, binary form"""

    def __init__(self, msg_type):
        self.msg_type = msg_type

    def format(self, row):
        """Format given row from csv.DictReader in binary original form"""

        def format_0183(row):
            if "raw_data" not in row.keys():
                return ""
            line = row["raw_data"]
            if line.endswith("<0D><0A>"):
                line = line.replace("<0D><0A>", "\r\n")
            # We don't care about spurious non-printable out of specs data
            return line

        def format_signalk(row):
            line = row["raw_data"] if "raw_data" in row.keys() else ""
            return line + "\r\n"

        if self.msg_type == "0183":
            return format_0183(row).encode()
        if self.msg_type == "signalk":
            return format_signalk(row).encode()
        raise NotImplementedError(self.msg_type)


class TcpServer:
    """Run a server which after being connected sends a bunch of messages"""

    def __init__(self, args, rows, progress_printer):
        self.args = args
        self.rows = rows
        self.progress_printer = progress_printer
        self.formatter = MsgFormat(args.messages)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((args.interface, args.port))
        if not args.quiet:
            print(f"Awaiting client connection at {args.interface}:{args.port}")
        self.socket.listen(1)
        self.connection, client = self.socket.accept()
        if not args.quiet:
            print(f"Connected to {client}")
        for i in range(args.count):
            if not args.quiet:
                print(f"Playing file {args.logfile.name} {i + 1}/{args.count}")
            self._send_rows()
            args.logfile.seek(0)
            self.rows.timestamp = -1
        self._close()

    def _close(self):
        """Close all sockets."""
        if not self.args.quiet:
            print("Closing connection")
        self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()

    def _send_rows(self):
        """Send all rows in given argument to connected client"""
        lines = 0
        for row in self.rows:
            lines += 1
            print(self.formatter.format(row).decode())
            self.connection.send(self.formatter.format(row).decode())
            self.progress_printer.report(lines)
        if not self.args.quiet:
            self.progress_printer.report(lines, True)
            print("")


class UdpClient:
    """Run a udp client which sends a bunch of messages"""

    def __init__(self, args, rows, progress_printer):
        self.args = args
        self.rows = rows
        self.progress_printer = progress_printer
        self.formatter = MsgFormat(args.messages)
        for i in range(args.count):
            if not args.quiet:
                print(f"Playing file {args.logfile.name} {i + 1}/{args.count}")
            self._send_rows()
            args.logfile.seek(0)
            self.rows.timestamp = -1

    def _send_rows(self):
        """Send all rows in given argument to (args.destination, args.port)"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        lines = 0
        for row in self.rows:
            dest = (self.args.destination, self.args.port)
            s.sendto(dest, self.formatter.format(row))
            lines += 1
            self.progress_printer.report(lines)
        if not self.args.quiet:
            self.progress_printer.report(lines, True)
            print("")


class SignalkServer:

    def __init__(self, args, rows, progress_printer):
        self.args = args
        self.rows = rows
        self.progress_printer = progress_printer
        self.formatter = MsgFormat(args.messages)

    async def handle_client(self, websocket):
        for i in range(self.args.count):
            if not self.args.quiet:
                print(
                    f"Playing file {self.args.logfile.name} {i + 1}/{self.args.count}"
                )
            await self._send_rows(websocket)
            self.args.logfile.seek(0)
            self.rows.timestamp = -1
        await websocket.close()
        asyncio.get_event_loop().stop()

    async def _send_rows(self, websocket):
        """Send all rows in given argument to connected client"""
        lines = 0
        for row in self.rows:
            await websocket.send(self.formatter.format(row))
            lines += 1
            await asyncio.sleep(0.2)  ## FIXME
            self.progress_printer.report(lines)
        if not self.args.quiet:
            self.progress_printer.report(lines, True)
            print("")

    @staticmethod
    def run(args, rows, progress_printer):

        async def do_run(args, rows, progress_printer):
            sk_server = SignalkServer(args, rows, progress_printer)
            handle_client = lambda websocket: sk_server.handle_client(websocket)
            while True:
                try:
                    async with serve(
                        handle_client, args.interface, args.port
                    ) as ws_server:
                        await ws_server.serve_forever()
                except EOFError:
                    print("EOFError in serve")
            await ws_server.wait_closed()

        while True:
            try:
                asyncio.run(do_run(args, rows, progress_printer))
            except RuntimeError:
                print("RuntimeError")
                continue
            except EOFError:
                print("EOFError in asyncio.run")
                continue


def get_args():
    """Return parsed arg_parser instance."""
    # fmt: off

    parser = argparse.ArgumentParser(description="OpenCPN logfile replay tool")
    parser.add_argument(
        "-r", "--role", choices=["tcp", "udp", "signalk"], default="tcp",
        help="Network role: tcp server, udp client or signalK source [tcp]"
    )
    parser.add_argument(
        "-m", "--messages", choices=["0183", "2000"],
        default="0183",
        help="Type of NMEA messages to play for tcp/udp roles [0183]",
    )
    parser.add_argument(
        "-p", "--port", metavar="port", default="2947", type=int,
        help="Local port (tcp) or destination port (udp) [2947]",
    )
    parser.add_argument(
        "-d", "--destination", metavar="destination", default="localhost",
        help="Udp destination hostname or ip address [localhost]",
    )
    parser.add_argument(
        "-i", "--interface", metavar="interface", default="localhost",
        help="TCP server interface hostname or IP address [localhost]",
    )
    parser.add_argument(
        "-c", "--count", metavar="count", type=int, default=1,
        help="Number of times to play input file [1]",
    )
    parser.add_argument(
        "-q", "--quiet", default=False, action="store_true",
        help="Run in quiet mode [false]",
    )
    parser.add_argument(
        "logfile", nargs="?", type=argparse.FileType("r"),
        default="monitor.csv",
        help="Log file created by Data Monitor in VDR mode [monitor.csv]",
    )
    args = parser.parse_args()
    if args.role == "signalk":
        args.messages = "signalk"
    return args

    # fmt: on


def main():
    """Indeed: main program"""

    args = get_args()
    log_file_reader = LogFileReader()
    log_file_reader.open(args.logfile)
    progress_printer = ProgressPrinter(args, log_file_reader.count_lines())
    csv_reader = csv.DictReader(log_file_reader)
    message_reader = MessageReader(csv_reader, args.messages)
    if args.role == "tcp":
        TcpServer(args, message_reader, progress_printer)
    elif args.role == "udp":
        UdpClient(args, message_reader, progress_printer)
    else:
        SignalkServer.run(args, message_reader, progress_printer)


if __name__ == "__main__":
    main()
