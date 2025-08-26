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
import binascii
import csv
import socket
import sys
import threading
import time

from datetime import datetime
from datetime import timezone

from websockets.sync.server import serve

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
            print(f"Sent {lines}/{self.total}\r", end="", flush=True)
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
        """Return number of data (not comments etc.) lines"""

        lines = 0
        for line in self:
            if len(line) > 3:
                # Hack handling signalk having the final '"' on a separate line
                lines += 1
        self.text_io_wrapper.seek(0)
        return lines - 1  # Don't include first line, the column specification


class MessageReader:
    """Iterable object, remove all rows created by csv.DictReader which
    does not match message type given to constructor. Add delays as defined
    by timestamps."""

    def __init__(self, source, message_type):
        self.message_type = message_type
        self.source = source
        self.timestamp = None

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
                return row
            if self.timestamp:
                time.sleep(timestamp - self.timestamp)
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
            return row["raw_data"] if "raw_data" in row.keys() else ""

        def format_2000(row):
            """Convert a line in the log to the Actisense ASCII formatA as of
            https://actisense.com/knowledge-base/nmea-2000/w2k-1-nmea-2000-to-wifi-gateway/nmea-2000-ascii-output-format/
            """

            if not row.keys() & {"received_at", "raw_data"}:
                return ""
            data = row["raw_data"]
            bytes_ = binascii.unhexlify(data.strip().replace(" ", ""))
            ps = bytes_[3]
            pf = bytes_[4]
            rdp = bytes_[5] & 0b011
            prio = bytes_[5] & 0b011100 >> 2
            if pf < 240:
                pgn = (rdp << 16) + (pf << 8) + 0
            else:
                pgn = (rdp << 16) + (pf << 8) + ps
            ms = row["received_at"]
            try:
                when = float(ms) / 1000
            except ValueError as exc:
                raise ValueError(f"Bad timestamp in line: {row}") from exc
            try:
                hms = datetime.fromtimestamp(when, timezone.utc)
            except OSError as exc:
                raise ValueError(f"Cannot convert timestamp {when}") from exc
            payload_size = int(row["raw_data"].split()[12], 16)
            payload: str = ""
            for w in row["raw_data"].split()[13 : 13 + payload_size]:
                payload += w
            rv = f"A{hms.hour:02d}{hms.minute:02d}{hms.second:02d}"
            rv += f".{int(hms.microsecond / 1000):03d}"
            rv += f" {ps:02X}{pf:02X}{prio:1X} {pgn:X} " + payload + "\r\n"
            return rv

        if self.msg_type == "0183":
            return format_0183(row).encode()
        if self.msg_type == "signalk":
            return format_signalk(row).encode()
        if self.msg_type == "2000":
            return format_2000(row).encode()

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
            server_address = f"{args.interface}:{args.port}"
            print(f"Awaiting client connection at {server_address}")
        self.socket.listen(1)
        self.connection, client = self.socket.accept()
        if not args.quiet:
            print(f"Connected to {client}")
        for i in range(args.count):
            if not args.quiet:
                print(f"Playing file {args.logfile.name} {i + 1}/{args.count}")
            self._send_rows()
            args.logfile.seek(0)
            self.rows.timestamp = None
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
            self.connection.send(self.formatter.format(row))
            lines += 1
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
            self.rows.timestamp = None

    def _send_rows(self):
        """Send all rows in given argument to (args.destination, args.port)"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        lines = 0
        for row in self.rows:
            dest = (self.args.destination, self.args.port)
            s.sendto(self.formatter.format(row), dest)
            lines += 1
            self.progress_printer.report(lines)
        if not self.args.quiet:
            self.progress_printer.report(lines, True)
            print("")


class SignalkServer:
    """SignalK websocket server which sends a bunch of messages"""

    def __init__(self, args, rows, progress_printer):
        self.args = args
        self.rows = rows
        self.progress_printer = progress_printer
        self.formatter = MsgFormat(args.messages)
        self.all_played_evt = threading.Event()

    def handle_client(self, websocket):
        """Handle client connection object"""
        for i in range(self.args.count):
            if not self.args.quiet:
                logfile = self.args.logfile.name
                print(f"Playing file {logfile} {i + 1}/{self.args.count}")
            self._send_rows(websocket)
            self.args.logfile.seek(0)
            self.rows.timestamp = None
        print("All played")
        websocket.close()
        self.all_played_evt.set()

    def _send_rows(self, websocket):
        """Send all rows in self.rows to connected client"""
        lines = 0
        for row in self.rows:
            try:
                websocket.send(self.formatter.format(row))
            except ValueError:
                print(f"bad logfile row (ignored): {row}", file=sys.stderr)
                continue
            lines += 1
            self.progress_printer.report(lines)
        if not self.args.quiet:
            self.progress_printer.report(lines, True)
            print("")

    @staticmethod
    def run(args, rows, progress_printer):
        """Actually run the websocket server, terminates after having sent
        all"""

        sk_server = SignalkServer(args, rows, progress_printer)
        ws_server = serve(sk_server.handle_client, args.interface, args.port)
        t = threading.Thread(target=ws_server.serve_forever)
        t.start()
        sk_server.all_played_evt.wait()
        ws_server.shutdown()
        t.join()


def get_args():
    """Return parsed arg_parser instance."""
    # fmt: off

    parser = argparse.ArgumentParser(
                        description="OpenCPN logfile replay tool",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "-r", "--role", choices=["tcp", "udp", "signalk"], default="tcp",
        help="Network role: tcp server, udp client or signalK source [tcp]",
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
